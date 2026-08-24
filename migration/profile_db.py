"""
Profile a database into a comparable fingerprint.

The same function runs against SQL Server and PostgreSQL and produces
the same shape of output, so verification is a diff of two dictionaries
rather than a person reading two screens and squinting.

What it captures, and what each one catches:

  row_counts          rows lost or duplicated in transit
  money_sums          cents lost to float, or truncated by numeric(19,2)
  date_ranges         timezone shifts — the min and max move by hours
  null_counts         NULLs silently converted to '' or 0
  merchant_profile    rows landing under the wrong parent
  status_counts       the collation trap, and mangled encodings
  max_ids             identity sequences left at the wrong high-water mark

Sums are read as strings and rebuilt as Decimal deliberately. Letting a
driver hand back a float would introduce the exact error the check exists
to detect.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

TABLES = ["merchants", "transactions", "batches", "settlements"]

MONEY_COLUMNS = {
    "transactions": ["amount", "fee_amount"],
    "batches": ["gross_amount"],
    "settlements": ["net_amount", "fee_amount", "gross_amount"],
}

DATE_COLUMNS = {
    "merchants": ["created_at", "updated_at"],
    "transactions": ["captured_at", "settled_at"],
    "batches": ["batch_date"],
    "settlements": ["settled_at"],
}

NULLABLE_COLUMNS = {
    "merchants": ["dba_name", "updated_at"],
    "transactions": ["card_last4", "auth_code", "settled_at"],
}

ID_COLUMNS = {
    "merchants": "merchant_id",
    "transactions": "txn_id",
    "batches": "batch_id",
    "settlements": "settlement_id",
}


def _rows(conn, sql: str) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(sql)
    out = cur.fetchall()
    cur.close()
    return [tuple(r) for r in out]


def _one(conn, sql: str) -> Any:
    r = _rows(conn, sql)
    return r[0][0] if r else None


def profile(conn, dialect: str) -> dict:
    """dialect is 'sqlserver' or 'postgres'."""
    if dialect not in {"sqlserver", "postgres"}:
        raise ValueError(f"unknown dialect: {dialect}")

    # SQL Server needs the dbo prefix; Postgres uses the search path.
    q = (lambda t: f"dbo.{t}") if dialect == "sqlserver" else (lambda t: t)

    # Cast sums to text in the database. A float round-trip through the
    # driver would introduce the error this is looking for.
    #
    # The double cast on SQL Server is not decoration. CAST(money AS
    # VARCHAR) rounds to TWO decimal places, but MONEY stores FOUR — so
    # the naive version silently truncates the measurement and reports a
    # difference that does not exist.
    #
    # Worse is the fix people reach for: rounding both sides to 2dp to
    # make them agree. That permanently blinds the check to a real
    # 4-decimal truncation, which is precisely the failure it exists to
    # detect. The measurement has to be at least as precise as the data.
    def as_text(expr: str) -> str:
        return (
            f"CAST(CAST({expr} AS DECIMAL(19,4)) AS VARCHAR(64))"
            if dialect == "sqlserver"
            else f"CAST({expr} AS text)"
        )

    result: dict[str, Any] = {"dialect": dialect}

    # ─── row counts ──────────────────────────────────────────────────
    result["row_counts"] = {t: _one(conn, f"SELECT COUNT(*) FROM {q(t)}") for t in TABLES}

    # ─── money sums, to the cent ─────────────────────────────────────
    sums: dict[str, str | None] = {}
    for table, cols in MONEY_COLUMNS.items():
        for col in cols:
            v = _one(conn, f"SELECT {as_text(f'SUM({col})')} FROM {q(table)}")
            sums[f"{table}.{col}"] = str(v) if v is not None else None
    result["money_sums"] = sums

    # ─── date ranges — where a timezone shift shows up ───────────────
    ranges: dict[str, dict[str, str | None]] = {}
    for table, cols in DATE_COLUMNS.items():
        for col in cols:
            row = _rows(conn, f"SELECT MIN({col}), MAX({col}) FROM {q(table)}")[0]
            ranges[f"{table}.{col}"] = {
                "min": _iso(row[0]),
                "max": _iso(row[1]),
            }
    result["date_ranges"] = ranges

    # ─── null counts — where a silent conversion shows up ────────────
    nulls: dict[str, int] = {}
    for table, cols in NULLABLE_COLUMNS.items():
        for col in cols:
            nulls[f"{table}.{col}"] = _one(
                conn, f"SELECT COUNT(*) FROM {q(table)} WHERE {col} IS NULL"
            )
    result["null_counts"] = nulls

    # ─── per-merchant profile — rows under the wrong parent ──────────
    # A migration can preserve the total row count and the grand total
    # while attaching rows to the wrong merchant. Only a per-parent
    # breakdown catches that.
    merchant_rows = _rows(
        conn,
        f"""
        SELECT merchant_id,
               COUNT(*),
               {as_text('SUM(amount)')}
        FROM {q('transactions')}
        GROUP BY merchant_id
        ORDER BY merchant_id
        """,
    )
    result["merchant_profile"] = {
        str(m): {"count": c, "sum_amount": str(s)} for m, c, s in merchant_rows
    }

    # ─── status distribution — the collation trap and encoding ───────
    # The COLLATE is essential, and its absence is a finding in itself.
    #
    # Under a case-insensitive collation, GROUP BY MERGES 'Captured',
    # 'captured' and 'CAPTURED' into a single group and labels it with
    # whichever spelling it encountered first. So SQL Server reports one
    # status where three exist, and the source database is structurally
    # unable to describe its own distinct values.
    #
    # Forcing a case-sensitive collation asks the question the profiler
    # actually meant to ask. The three counts then sum exactly to the one
    # merged count, which is what proves the data is intact.
    group_by = (
        "txn_status COLLATE SQL_Latin1_General_CP1_CS_AS"
        if dialect == "sqlserver"
        else "txn_status"
    )
    status_rows = _rows(
        conn,
        f"""
        SELECT {group_by}, COUNT(*)
        FROM {q('transactions')}
        GROUP BY {group_by}
        ORDER BY 1
        """,
    )
    result["status_counts"] = {str(s): c for s, c in status_rows}

    # ─── the collation comparison, as two numbers ────────────────────
    if dialect == "sqlserver":
        ci = _one(
            conn,
            f"SELECT COUNT(*) FROM {q('transactions')} WHERE txn_status = 'Captured'",
        )
        cs = _one(
            conn,
            f"""SELECT COUNT(*) FROM {q('transactions')}
                WHERE txn_status COLLATE SQL_Latin1_General_CP1_CS_AS = 'Captured'""",
        )
    else:
        # Postgres is case-sensitive by default, so the plain equality IS
        # the case-sensitive number. lower() gives the insensitive one.
        cs = _one(
            conn,
            f"SELECT COUNT(*) FROM {q('transactions')} WHERE txn_status = 'Captured'",
        )
        ci = _one(
            conn,
            f"SELECT COUNT(*) FROM {q('transactions')} WHERE lower(txn_status) = 'captured'",
        )
    result["collation"] = {"case_insensitive": ci, "case_sensitive": cs}

    # ─── identity high-water marks ───────────────────────────────────
    result["max_ids"] = {
        t: _one(conn, f"SELECT MAX({c}) FROM {q(t)}") for t, c in ID_COLUMNS.items()
    }

    # ─── unicode survival ────────────────────────────────────────────
    #
    # Encoding damage during the load shows up here and in no aggregate.
    #
    # The N prefix is required on SQL Server and its absence is silent.
    # Without it a literal is treated as VARCHAR, downconverted to the
    # database's non-Unicode codepage, and every non-ASCII character
    # becomes '?' — so the LIKE matches nothing and the check reports a
    # clean pass over an empty result set.
    n = "N" if dialect == "sqlserver" else ""
    result["unicode_sample"] = [
        r[0]
        for r in _rows(
            conn,
            f"""SELECT legal_name FROM {q('merchants')}
                WHERE legal_name LIKE {n}'%北方%'
                   OR legal_name LIKE {n}'%Трейд%'
                   OR legal_name LIKE {n}'%Móvil%'
                ORDER BY legal_name""",
        )
    ]

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
    import sys

    from db import postgres_connection, sqlserver_connection

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dialect", choices=["sqlserver", "postgres"])
    p.add_argument("-o", "--out", default=None)
    a = p.parse_args()

    conn = sqlserver_connection() if a.dialect == "sqlserver" else postgres_connection()
    try:
        prof = profile(conn, a.dialect)
    finally:
        conn.close()

    out = a.out or f"{a.dialect}_profile.json"
    write(prof, out)

    print(f"wrote {out}\n")
    for table, n in prof["row_counts"].items():
        print(f"  {table:<14} {n:>10,}")
    print()
    print(f"  collation, case-insensitive  {prof['collation']['case_insensitive']:>10,}")
    print(f"  collation, case-sensitive    {prof['collation']['case_sensitive']:>10,}")
    print()
    for name, total in prof["money_sums"].items():
        print(f"  {name:<26} {total}")
    print(file=sys.stderr)
