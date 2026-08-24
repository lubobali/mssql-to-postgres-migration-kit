#!/usr/bin/env python3
"""
Generate the legacy payments dataset.

No real data is used anywhere in this project. Every merchant, card
suffix and auth code below is synthetic.

More importantly: this data is *designed to break things*. A downloaded
dataset would not contain the edge cases a SQL Server to PostgreSQL
migration actually fails on, so each one is planted deliberately and
documented against the trap it exercises.

Writes pipe-delimited files for BULK INSERT. Pipe rather than comma
because merchant names contain commas, and the moment a loader has to
reason about quoting is the moment a migration develops a mystery.
"""

from __future__ import annotations

import argparse
import csv
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Fixed seed. The dataset must be identical on every run, or the
# verification baseline means nothing.
SEED = 20260824
random.seed(SEED)

OUT = Path(__file__).parent / "data"

# ─────────────────────────────────────────────────────────────────────
#  TRAP: unicode in NVARCHAR columns.
#
#  SQL Server needs NVARCHAR and an N'' prefix for these. PostgreSQL is
#  UTF-8 natively and needs neither. If the migration mangles encoding,
#  it shows up here and nowhere else.
# ─────────────────────────────────────────────────────────────────────
NAME_PARTS_A = [
    "Northgate", "Pinnacle", "Ironwood", "Blue Harbor", "Cascadia",
    "Meridian", "Copperline", "Riverstone", "Halcyon", "Brightwater",
    "Zürich",          # umlaut
    "Café Móvil",      # accents
    "Balkan Трейд",    # Cyrillic
    "北方物流",          # CJK
    "Škoda Parts",     # caron
]

NAME_PARTS_B = [
    "Industrial Supply", "Freight Systems", "Wholesale Group",
    "Distribution Co", "Logistics LLC", "Trading Partners",
    "Manufacturing Inc", "Equipment Rental", "Building Materials",
    "Auto Parts, Inc.",  # embedded comma — hence pipe delimiters
]

MCC_CODES = ["5045", "5065", "5085", "5111", "5199", "5251", "7372", "8911"]

# ─────────────────────────────────────────────────────────────────────
#  TRAP: mixed case.
#
#  SQL Server's default collation is case-INSENSITIVE. PostgreSQL is
#  case-SENSITIVE. So WHERE txn_status = 'captured' returns everything
#  on the source and a subset on the target — with no error at all.
#
#  This is the most dangerous entry in the file, because nothing fails.
#  The query just quietly returns different rows.
# ─────────────────────────────────────────────────────────────────────
TXN_STATUSES = ["Captured", "captured", "CAPTURED", "Authorized", "Voided", "Refunded"]
ONBOARDING_STATUSES = ["Active", "active", "PENDING", "Pending", "Suspended"]
BATCH_STATUSES = ["Settled", "settled", "Pending", "Failed"]


def make_amount() -> Decimal:
    """
    TRAP: MONEY has a scale of 4 and exact decimal semantics.

    The correct PostgreSQL target is numeric(19,4). Anything that routes
    through a float loses cents — and in payments a lost cent is not a
    rounding detail, it is a wrong number that someone eventually has to
    reconcile.

    These values are chosen to make float error visible rather than
    theoretical:
      - classic binary-representation offenders (0.1, 0.7, 29.99)
      - amounts with 4-decimal precision, which MONEY keeps and a
        careless numeric(19,2) target silently truncates
      - one cent, where any rounding error is 100% of the value
      - large values, where float precision degrades
    """
    style = random.random()

    if style < 0.03:
        return Decimal("0.01")
    if style < 0.06:
        return Decimal(random.choice(["0.10", "0.70", "29.99", "1.15", "8.20"]))
    if style < 0.12:
        # 4 decimal places — survives MONEY, dies against numeric(19,2)
        return Decimal(f"{random.randint(1, 9999)}.{random.randint(0, 9999):04d}")
    if style < 0.15:
        return Decimal(f"{random.randint(50_000, 200_000)}.{random.randint(0, 99):02d}")

    return Decimal(f"{random.randint(1, 5000)}.{random.randint(0, 99):02d}")


def make_timestamp(base: datetime) -> datetime:
    """
    TRAP: DATETIME2 carries no timezone.

    Reading it as local time instead of UTC shifts every value. The
    error is invisible in the middle of a day and obvious at the edges,
    so the distribution is weighted toward the edges:

      - just before and just after midnight UTC, where a wrong offset
        moves the date to a different day
      - across the US DST boundaries, where a fixed offset is wrong for
        half the year
    """
    style = random.random()

    if style < 0.10:
        # within a minute of midnight UTC
        day = base + timedelta(days=random.randint(0, 240))
        return day.replace(
            hour=0, minute=0,
            second=random.randint(0, 59),
            microsecond=random.randint(0, 999) * 1000,
        ) - timedelta(seconds=random.choice([0, 30]))

    if style < 0.16:
        # around US DST transitions in 2026
        dst = random.choice([datetime(2026, 3, 8, 2, 30), datetime(2026, 11, 1, 1, 30)])
        return dst + timedelta(minutes=random.randint(-90, 90))

    return base + timedelta(
        days=random.randint(0, 240),
        seconds=random.randint(0, 86_399),
        milliseconds=random.randint(0, 999),
    )


def fmt_ts(ts: datetime) -> str:
    """DATETIME2(3) — millisecond precision, no timezone marker."""
    return ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


def generate(n_merchants: int, n_transactions: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)

    # ─── merchants ───────────────────────────────────────────────────
    merchants = []
    with (OUT / "merchants.psv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\")
        for i in range(1, n_merchants + 1):
            legal = f"{random.choice(NAME_PARTS_A)} {random.choice(NAME_PARTS_B)}"

            # TRAP: NULL in a nullable column. A conversion that turns
            # NULL into an empty string is invisible except in a
            # per-column NULL count.
            dba = "" if random.random() < 0.25 else f"{legal.split()[0]} Direct"

            created = make_timestamp(base - timedelta(days=400))
            merchants.append(i)

            w.writerow([
                i,
                str(uuid.UUID(int=random.getrandbits(128))),  # string form is byte-order safe
                legal,
                dba,
                random.choice(MCC_CODES),
                1 if random.random() < 0.85 else 0,           # BIT, not boolean
                random.choice(ONBOARDING_STATUSES),
                fmt_ts(created),
                "" if random.random() < 0.4 else fmt_ts(created + timedelta(days=30)),
            ])

    # ─── transactions ────────────────────────────────────────────────
    with (OUT / "transactions.psv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\")
        for i in range(1, n_transactions + 1):
            amount = make_amount()

            # Refunds are negative. A migration that mishandles sign, or
            # a target column defined as unsigned, fails here.
            if random.random() < 0.04:
                amount = -amount

            fee = (abs(amount) * Decimal("0.0295")).quantize(Decimal("0.0001"))
            captured = make_timestamp(base)
            status = random.choice(TXN_STATUSES)

            # TRAP: settled_at is NULL for anything not yet settled.
            settled = ""
            if status.lower() == "captured" and random.random() < 0.8:
                settled = fmt_ts(captured + timedelta(days=random.randint(1, 3)))

            w.writerow([
                i,
                random.choice(merchants),
                f"{amount:.4f}",
                f"{fee:.4f}",
                random.choice(["USD", "USD", "USD", "CAD", "EUR"]),
                "" if random.random() < 0.05 else f"{random.randint(0, 9999):04d}",
                "" if random.random() < 0.05 else f"{random.randint(100000, 999999)}",
                status,
                fmt_ts(captured),
                settled,
            ])

    print(f"merchants     {n_merchants:>9,}   {OUT / 'merchants.psv'}")
    print(f"transactions  {n_transactions:>9,}   {OUT / 'transactions.psv'}")
    print()
    print("Traps planted: unicode, embedded commas, mixed-case status, NULLs,")
    print("4-decimal amounts, one-cent amounts, negatives, midnight-UTC and")
    print("DST-boundary timestamps.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--merchants", type=int, default=50)
    p.add_argument("--transactions", type=int, default=250_000)
    a = p.parse_args()
    generate(a.merchants, a.transactions)
