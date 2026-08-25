"""
Profile a database into a comparable fingerprint.

The same function runs against SQL Server and PostgreSQL and returns the
same shape, so verification is a diff of two dictionaries rather than a
person reading two screens and squinting.

Which columns get which check comes from schema.json. A column declared
`money` gets summed exactly; a `timestamp` gets its min and max compared;
a nullable column gets a NULL count. That is why the config declares
types rather than just names.

What each check catches:

  row_counts          rows lost or duplicated in transit
  money_sums          cents lost to float, or truncated by numeric(19,2)
  date_ranges         timezone shifts — min and max move by hours
  null_counts         NULLs silently converted to '' or 0
  parent_profile      rows landing under the wrong parent
  status_counts       the collation trap, and mangled encodings
  max_ids             identity sequences left at the wrong high-water mark
  unicode_sample      encoding damage, which shows up in no aggregate
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import schema as S


def _rows(conn, sql: str) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql)
    out = cur.fetchall()
    cur.close()
    return [tuple(r) for r in out]


def _one(conn, sql: str) -> Any:
    r = _rows(conn, sql)
    return r[0][0] if r else None


def profile(conn, dialect: str, cfg: dict | None = None) -> dict:
    """dialect is 'sqlserver' or 'postgres'."""
    if dialect not in {"sqlserver", "postgres"}:
        raise ValueError(f"unknown dialect: {dialect}")

    cfg = cfg or S.load()
    q = lambda name: S.qualify(cfg, dialect, name)  # noqa: E731

    # Cast sums to text in the database. A float round-trip through the
    # driver would introduce the error this is looking for.
    #
    # The double cast on SQL Server is not decoration. CAST(money AS
    # VARCHAR) rounds to TWO decimal places while MONEY stores FOUR, so
    # the naive version truncates the MEASUREMENT and reports a
    # difference that does not exist.
    #
    # Worse is the fix people reach for: rounding both sides to 2dp so
    # they agree. That permanently blinds the check to a real 4-decimal
    # truncation, which is precisely the failure it exists to detect. A
    # measurement has to be at least as precise as the thing it measures.
    def as_text(expr: str) -> str:
        return (
            f"CAST(CAST({expr} AS DECIMAL(19,4)) AS VARCHAR(64))"
            if dialect == "sqlserver"
            else f"CAST({expr} AS text)"
        )

    # Non-ASCII literals need the N prefix on SQL Server, and its absence
    # is silent: the literal is treated as VARCHAR, downconverted to the
    # database's non-Unicode codepage, every non-ASCII character becomes
    # '?', the LIKE matches nothing, and the check reports a clean pass
    # over an empty result set.
    n = "N" if dialect == "sqlserver" else ""

    result: dict[str, Any] = {"dialect": dialect}

    # ─── row counts ──────────────────────────────────────────────────
    result["row_counts"] = {
        t: _one(conn, f"SELECT COUNT(*) FROM {q(t)}") for t in S.table_names(cfg)
    }

    # ─── money sums, exact ───────────────────────────────────────────
    sums: dict[str, str | None] = {}
    for tbl, cols in S.money_columns(cfg).items():
        for col in cols:
            v = _one(conn, f"SELECT {as_text(f'SUM({col})')} FROM {q(tbl)}")
            sums[f"{tbl}.{col}"] = str(v) if v is not None else None
    result["money_sums"] = sums

    # ─── date ranges — where a timezone shift shows up ───────────────
    ranges: dict[str, dict[str, str | None]] = {}
    for tbl, cols in S.date_columns(cfg).items():
        for col in cols:
            lo, hi = _rows(conn, f"SELECT MIN({col}), MAX({col}) FROM {q(tbl)}")[0]
            ranges[f"{tbl}.{col}"] = {"min": _iso(lo), "max": _iso(hi)}
    result["date_ranges"] = ranges

    # ─── null counts — where a silent conversion shows up ────────────
    nulls: dict[str, int] = {}
    for tbl, cols in S.nullable_columns(cfg).items():
        for col in cols:
            nulls[f"{tbl}.{col}"] = _one(
                conn, f"SELECT COUNT(*) FROM {q(tbl)} WHERE {col} IS NULL"
            )
    result["null_counts"] = nulls

    # ─── per-parent profile — rows under the wrong parent ────────────
    # A migration can preserve the total row count AND the grand total
    # while attaching rows to the wrong parent. Only this catches it.
    link = S.profiled_parent(cfg)
    if link:
        child, fk, money = link
        rows = _rows(
            conn,
            f"""SELECT {fk}, COUNT(*), {as_text(f'SUM({money})')}
                FROM {q(child)} GROUP BY {fk} ORDER BY {fk}""",
        )
        result["parent_profile"] = {
            str(k): {"count": c, "sum_amount": str(s)} for k, c, s in rows
        }
        result["_parent_link"] = {"child": child, "fk": fk, "money": money}
    else:
        result["parent_profile"] = {}

    # ─── the collation check ─────────────────────────────────────────
    cc = cfg.get("collation_check")
    if cc:
        tbl, col, val = cc["table"], cc["column"], cc["value"]

        # The COLLATE is essential, and its absence is a finding in
        # itself. Under a case-insensitive collation GROUP BY MERGES the
        # case variants into one group and labels it with whichever
        # spelling it met first — so the source database is structurally
        # unable to describe its own distinct values.
        group = (
            f"{col} COLLATE SQL_Latin1_General_CP1_CS_AS"
            if dialect == "sqlserver"
            else col
        )
        result["status_counts"] = {
            str(s): c
            for s, c in _rows(
                conn,
                f"SELECT {group}, COUNT(*) FROM {q(tbl)} GROUP BY {group} ORDER BY 1",
            )
        }

        if dialect == "sqlserver":
            ci = _one(conn, f"SELECT COUNT(*) FROM {q(tbl)} WHERE {col} = {n}'{val}'")
            cs = _one(
                conn,
                f"""SELECT COUNT(*) FROM {q(tbl)}
                    WHERE {col} COLLATE SQL_Latin1_General_CP1_CS_AS = {n}'{val}'""",
            )
        else:
            # Postgres is case-sensitive by default, so plain equality IS
            # the case-sensitive number. lower() gives the other one.
            cs = _one(conn, f"SELECT COUNT(*) FROM {q(tbl)} WHERE {col} = '{val}'")
            ci = _one(
                conn,
                f"SELECT COUNT(*) FROM {q(tbl)} WHERE lower({col}) = lower('{val}')",
            )
        result["collation"] = {"case_insensitive": ci, "case_sensitive": cs}
    else:
        result["status_counts"] = {}
        result["collation"] = {}

    # ─── identity high-water marks ───────────────────────────────────
    result["max_ids"] = {
        t: _one(conn, f"SELECT MAX({pk}) FROM {q(t)}")
        for t, pk in S.id_columns(cfg).items()
    }

    # ─── unicode survival ────────────────────────────────────────────
    uc = cfg.get("unicode_check")
    if uc:
        where = " OR ".join(f"{uc['column']} LIKE {n}'{p}'" for p in uc["patterns"])
        result["unicode_sample"] = [
            r[0]
            for r in _rows(
                conn,
                f"""SELECT {uc['column']} FROM {q(uc['table'])}
                    WHERE {where} ORDER BY {uc['column']}""",
            )
        ]
    else:
        result["unicode_sample"] = []

    return result


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


class _Encoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)


def write(profile_dict: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(profile_dict, fh, indent=2, ensure_ascii=False, cls=_Encoder)


if __name__ == "__main__":
    import argparse

    from db import postgres_connection, sqlserver_connection

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dialect", choices=["sqlserver", "postgres"])
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--schema", default=None)
    a = p.parse_args()

    cfg = S.load(a.schema)
    conn = sqlserver_connection() if a.dialect == "sqlserver" else postgres_connection()
    try:
        prof = profile(conn, a.dialect, cfg)
    finally:
        conn.close()

    write(prof, a.out or f"{a.dialect}_profile.json")

    print(f"wrote {a.out or f'{a.dialect}_profile.json'}\n")
    for tbl, n in prof["row_counts"].items():
        print(f"  {tbl:<14} {n:>10,}")
    if prof["collation"]:
        print()
        print(f"  collation, case-insensitive  {prof['collation']['case_insensitive']:>10,}")
        print(f"  collation, case-sensitive    {prof['collation']['case_sensitive']:>10,}")
    print()
    for name, total in prof["money_sums"].items():
        print(f"  {name:<26} {total}")
