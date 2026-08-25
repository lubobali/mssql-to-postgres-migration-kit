#!/usr/bin/env python3
"""
Move the data: SQL Server → PostgreSQL.

This is the manual path. AWS DMS is what a real cutover uses and is set up
separately, but doing it by hand once is how you learn what DMS is doing
underneath and where its defaults would have hurt you.

The tables come from schema.json, so this works on any schema without
editing SQL. What each column NEEDS doing to it comes from its declared
type — which is the point of declaring types rather than just names.

Three properties this has to have:

  IDEMPOTENT   re-runnable without duplicating anything, because the
               correct response to a failed verification is to fix the
               pipeline and reload, never to patch rows on the target

  TYPED        every value crosses as a Python object of the right type,
               not as text that gets re-parsed. A float anywhere in this
               path reintroduces the error the project exists to detect

  EXPLICIT     the timezone assumption lives in one place rather than
               being inherited from whatever the server happens to be set
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from decimal import Decimal

import schema as S
from db import postgres_connection, sqlserver_connection

BATCH = 5_000


def make_transform(types: list[str], tz):
    """
    Build the per-row converter from the declared column types.

    Each conversion exists for a reason worth stating:

      money      pyodbc hands MONEY back as Decimal and it stays Decimal
                 into numeric(19,4). No float touches the value, ever.
      bool       BIT is 0/1. Made explicit rather than trusting the driver.
      uuid       already a string, because the SELECT cast it. The byte
                 form differs between engines; the text form does not.
      timestamp  DATETIME2 has no timezone, so the source zone is ATTACHED
                 here, never converted. Guessing wrong shifts every value.
    """

    def convert(value, kind):
        if value is None:
            return None
        if kind == "money":
            return Decimal(str(value))
        if kind == "bool":
            return bool(value)
        if kind == "timestamp":
            if not isinstance(value, datetime):
                return value
            return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
        return value

    def transform(row):
        return tuple(convert(v, k) for v, k in zip(row, types))

    return transform


def migrate(cfg: dict, truncate: bool = True) -> None:
    src = sqlserver_connection()
    dst = postgres_connection()
    tz = S.source_timezone(cfg)
    names = S.table_names(cfg)

    try:
        with dst.cursor() as cur:
            if truncate:
                # Reverse order so foreign keys hold. RESTART IDENTITY also
                # resets the sequences, so a reload does not inherit the
                # previous run's high-water mark.
                print("clearing target")
                cur.execute(
                    f"TRUNCATE {', '.join(reversed(names))} RESTART IDENTITY CASCADE"
                )
                dst.commit()

            for name in names:
                t = S.table(cfg, name)
                transform = make_transform(S.migratable_types(t), tz)
                select, insert = S.select_sql(cfg, t), S.insert_sql(cfg, t)

                started = time.monotonic()
                scur = src.cursor()
                scur.execute(select)

                moved = 0
                while True:
                    rows = scur.fetchmany(BATCH)
                    if not rows:
                        break
                    cur.executemany(insert, [transform(tuple(r)) for r in rows])
                    moved += len(rows)
                    print(f"  {name:<14} {moved:>10,}", end="\r", flush=True)

                scur.close()
                dst.commit()
                secs = time.monotonic() - started
                rate = moved / secs if secs else 0
                print(f"  {name:<14} {moved:>10,}   {secs:6.1f}s   {rate:>9,.0f} rows/s")

            # ─── the identity high-water mark ────────────────────────
            #
            # Explicit keys were supplied for every row, so the sequences
            # still sit where TRUNCATE left them and the next real INSERT
            # would collide on the primary key.
            #
            # Nothing warns about this. It surfaces as a duplicate key
            # error on the first write after go-live, which is a
            # spectacularly bad time to find it.
            print("\nresetting identity sequences to the high-water mark")
            for name, pk in S.id_columns(cfg).items():
                cur.execute(
                    f"""SELECT setval(
                            pg_get_serial_sequence('{name}', '{pk}'),
                            COALESCE((SELECT MAX({pk}) FROM {name}), 1),
                            true)"""
                )
                print(f"  {name:<14} -> {cur.fetchone()[0]:,}")
            dst.commit()

            # Fresh statistics, or the first EXPLAIN after migration is
            # planned against what the planner thinks is an empty table.
            print("\nANALYZE")
            cur.execute("ANALYZE")
            dst.commit()

    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--schema", default=None, help="path to schema.json")
    p.add_argument(
        "--append",
        action="store_true",
        help="skip the truncate (not the normal path — a reload starts clean)",
    )
    a = p.parse_args()

    t0 = time.monotonic()
    migrate(S.load(a.schema), truncate=not a.append)
    print(f"\ndone in {time.monotonic() - t0:.1f}s", file=sys.stderr)
