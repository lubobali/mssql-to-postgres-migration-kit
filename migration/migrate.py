"""
Move the data: SQL Server → PostgreSQL.

This is the manual path. AWS DMS is the tool a real cutover uses, and it
is set up separately — but doing it by hand once is how you learn what
DMS is doing underneath, and where its defaults would have hurt you.

Three properties this has to have:

  IDEMPOTENT     re-runnable without duplicating anything, because the
                 correct response to a failed verification is to fix the
                 pipeline and reload, never to patch rows on the target

  TYPED          every value crosses as a Python object with the right
                 type, not as text that gets re-parsed. A float anywhere
                 in this path reintroduces the error the whole project
                 exists to detect

  EXPLICIT       the timezone assumption is written down in one place
                 rather than inherited from whatever the server happens
                 to be set to
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

from db import postgres_connection, sqlserver_connection

# ─────────────────────────────────────────────────────────────────────
#  The timezone decision, in one place.
#
#  DATETIME2 carries no timezone. The source system wrote UTC — that is
#  a fact about the application, not something the database can tell us,
#  and it is exactly the kind of assumption that has to be stated rather
#  than guessed.
#
#  Guess it wrong and every timestamp shifts by the offset, which moves
#  dates across midnight and changes which day a settlement belongs to.
#  The date_ranges check exists to catch precisely this.
# ─────────────────────────────────────────────────────────────────────
SOURCE_TZ = timezone.utc


def utc(value: datetime | None) -> datetime | None:
    """Attach the source timezone. Never convert — attach."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(SOURCE_TZ)
    return value.replace(tzinfo=SOURCE_TZ)


TABLES = [
    (
        "merchants",
        """SELECT merchant_id,
                  CAST(merchant_guid AS VARCHAR(36)),
                  legal_name, dba_name, mcc, is_active,
                  onboarding_status, created_at, updated_at
           FROM dbo.merchants ORDER BY merchant_id""",
        """INSERT INTO merchants
             (merchant_id, merchant_guid, legal_name, dba_name, mcc,
              is_active, onboarding_status, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        lambda r: (
            r[0],
            # TRAP 2 — UNIQUEIDENTIFIER byte order. SQL Server stores the
            # first three fields little-endian and Postgres uuid is
            # big-endian, so moving raw bytes scrambles the value. Cast
            # to VARCHAR on the source and let Postgres parse the string:
            # the text form is identical on both sides.
            r[1],
            r[2],
            r[3],
            r[4],
            # TRAP 5 — BIT to boolean, made explicit rather than relying
            # on the driver to guess.
            bool(r[5]),
            r[6],
            utc(r[7]),
            utc(r[8]),
        ),
    ),
    (
        "transactions",
        """SELECT txn_id, merchant_id, amount, fee_amount, currency,
                  card_last4, auth_code, txn_status, captured_at, settled_at
           FROM dbo.transactions ORDER BY txn_id""",
        """INSERT INTO transactions
             (txn_id, merchant_id, amount, fee_amount, currency,
              card_last4, auth_code, txn_status, captured_at, settled_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        lambda r: (
            r[0],
            r[1],
            # TRAP 8 — MONEY. pyodbc hands MONEY back as Decimal, and it
            # stays Decimal all the way into numeric(19,4). No float
            # touches this value at any point.
            Decimal(str(r[2])),
            Decimal(str(r[3])),
            r[4],
            r[5],
            r[6],
            # Deliberately NOT normalised. Cleaning the case here would
            # hide the collation finding, which is the thing worth
            # discovering. The fix belongs in a follow-up migration,
            # applied knowingly.
            r[7],
            utc(r[8]),
            utc(r[9]),
        ),
    ),
    (
        "batches",
        """SELECT batch_id, merchant_id, batch_date, txn_count,
                  gross_amount, batch_status
           FROM dbo.batches ORDER BY batch_id""",
        """INSERT INTO batches
             (batch_id, merchant_id, batch_date, txn_count,
              gross_amount, batch_status)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        lambda r: (r[0], r[1], r[2], r[3], Decimal(str(r[4])), r[5]),
    ),
    (
        "settlements",
        # gross_amount is omitted deliberately: it is GENERATED ALWAYS on
        # the target, so Postgres computes it. Supplying it would error.
        # This is TRAP 11 — a computed column that has to be recognised
        # as computed rather than copied.
        """SELECT settlement_id, batch_id, net_amount, fee_amount, settled_at
           FROM dbo.settlements ORDER BY settlement_id""",
        """INSERT INTO settlements
             (settlement_id, batch_id, net_amount, fee_amount, settled_at)
           VALUES (%s,%s,%s,%s,%s)""",
        lambda r: (r[0], r[1], Decimal(str(r[2])), Decimal(str(r[3])), utc(r[4])),
    ),
]

BATCH = 5_000


def migrate(truncate: bool = True) -> None:
    src = sqlserver_connection()
    dst = postgres_connection()

    try:
        with dst.cursor() as cur:
            if truncate:
                # Idempotency. RESTART IDENTITY also resets the sequences,
                # so a reload does not inherit the previous run's
                # high-water mark.
                print("clearing target")
                cur.execute(
                    "TRUNCATE settlements, batches, transactions, merchants "
                    "RESTART IDENTITY CASCADE"
                )
                dst.commit()

            for name, select, insert, transform in TABLES:
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

            # ─── TRAP 1 — the identity high-water mark ───────────────
            #
            # Explicit keys were supplied for every row, so the sequences
            # still sit where TRUNCATE left them. The next real INSERT
            # would collide on the primary key.
            #
            # Nothing warns you about this. It surfaces as a duplicate
            # key error on the first write after go-live, which is a
            # spectacularly bad time to find it.
            print("\nresetting identity sequences to the high-water mark")
            for table, col in [
                ("merchants", "merchant_id"),
                ("transactions", "txn_id"),
                ("batches", "batch_id"),
                ("settlements", "settlement_id"),
            ]:
                cur.execute(
                    f"""SELECT setval(
                            pg_get_serial_sequence('{table}', '{col}'),
                            COALESCE((SELECT MAX({col}) FROM {table}), 1),
                            true)"""
                )
                print(f"  {table:<14} -> {cur.fetchone()[0]:,}")
            dst.commit()

            # Fresh statistics, or the first EXPLAIN after migration is
            # planned against an empty table.
            print("\nANALYZE")
            cur.execute("ANALYZE")
            dst.commit()

    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--append",
        action="store_true",
        help="skip the truncate (not the normal path — a reload should start clean)",
    )
    a = p.parse_args()

    t0 = time.monotonic()
    migrate(truncate=not a.append)
    print(f"\ndone in {time.monotonic() - t0:.1f}s", file=sys.stderr)
