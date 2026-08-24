"""
Migration verification.

This suite is a gate, not a report. Nothing goes live until it is green,
and the correct response to a failure is to fix the pipeline and reload —
never to patch rows on the target, which produces a database that matches
nothing and cannot be proven.

Failures fall into three classes, and they need different responses:

  1. Rows missing              the load failed partway     → fix, wipe, reload
  2. Values differ             type mapping bug            → fix mapping, reload
  3. Query results differ      NOT a data bug              → reloading never fixes it

The third class is the one people get wrong. See the collation tests at
the bottom: the data is byte-for-byte identical on both sides, and a
WHERE clause still returns a third of the rows it used to. Re-running the
migration a hundred times will not change that number, because nothing
about the data is wrong.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# ─────────────────────────────────────────────────────────────────────
#  Class 1 — did every row arrive
# ─────────────────────────────────────────────────────────────────────


def test_row_counts_match(source: dict, target: dict) -> None:
    """The cheapest check, and the one that catches a load that died halfway."""
    assert target["row_counts"] == source["row_counts"]


def test_no_orphaned_foreign_keys(pg) -> None:
    """
    Referential integrity survived.

    A bulk load with constraints disabled for speed can leave children
    pointing at parents that never arrived. The row counts still match.
    """
    orphans = pg.execute(
        """
        SELECT COUNT(*) FROM transactions t
        LEFT JOIN merchants m ON m.merchant_id = t.merchant_id
        WHERE m.merchant_id IS NULL
        """
    ).fetchone()[0]
    assert orphans == 0, f"{orphans:,} transactions reference a missing merchant"


# ─────────────────────────────────────────────────────────────────────
#  Class 2 — did every value arrive intact
# ─────────────────────────────────────────────────────────────────────


def test_money_sums_match_exactly(source: dict, target: dict) -> None:
    """
    Every money column, summed, compared as Decimal.

    Not approximately. Not to two decimal places. Exactly.

    MONEY carries four decimals, so a target of numeric(19,2) truncates
    silently and the loss only becomes visible in an aggregate across
    250,000 rows. Comparing at 2dp would hide precisely that.
    """
    mismatches = []
    for column, src_total in source["money_sums"].items():
        tgt_total = target["money_sums"].get(column)
        if src_total is None and tgt_total is None:
            continue
        if Decimal(src_total) != Decimal(tgt_total):
            delta = Decimal(tgt_total) - Decimal(src_total)
            mismatches.append(f"  {column}: {src_total} -> {tgt_total}  (delta {delta})")

    assert not mismatches, "money totals differ:\n" + "\n".join(mismatches)


def test_date_ranges_match(source: dict, target: dict) -> None:
    """
    Minimum and maximum of every date column.

    This is the timezone check. A wrong offset assumption shifts every
    timestamp by the same amount, which leaves row counts and sums
    perfectly intact and moves the earliest and latest values by hours —
    enough to change which day a settlement belongs to.
    """
    mismatches = []
    for column, src_range in source["date_ranges"].items():
        tgt_range = target["date_ranges"].get(column, {})
        for bound in ("min", "max"):
            s, t = src_range.get(bound), tgt_range.get(bound)
            if s is None and t is None:
                continue
            if not _same_instant(s, t):
                mismatches.append(f"  {column}.{bound}: {s} -> {t}")

    assert not mismatches, "date ranges shifted:\n" + "\n".join(mismatches)


def test_null_counts_match(source: dict, target: dict) -> None:
    """
    NULLs per nullable column.

    A conversion that turns NULL into an empty string or a zero preserves
    the row count, and preserves the sum if the value was zero anyway.
    This is the only check that sees it.
    """
    assert target["null_counts"] == source["null_counts"]


def test_per_merchant_profile_matches(source: dict, target: dict) -> None:
    """
    Count and total per merchant.

    A migration can preserve the total row count AND the grand total
    while attaching rows to the wrong parent — the aggregate is identical
    and every individual merchant's balance is wrong. Only a per-parent
    breakdown catches it.
    """
    src, tgt = source["merchant_profile"], target["merchant_profile"]

    assert set(tgt) == set(src), "the set of merchants with transactions changed"

    mismatches = []
    for merchant_id, s in src.items():
        t = tgt[merchant_id]
        if t["count"] != s["count"]:
            mismatches.append(
                f"  merchant {merchant_id}: count {s['count']:,} -> {t['count']:,}"
            )
        if Decimal(s["sum_amount"]) != Decimal(t["sum_amount"]):
            mismatches.append(
                f"  merchant {merchant_id}: total {s['sum_amount']} -> {t['sum_amount']}"
            )

    assert not mismatches, "per-merchant totals differ:\n" + "\n".join(mismatches)


def test_unicode_survived(source: dict, target: dict) -> None:
    """
    Non-ASCII merchant names, matched exactly.

    Encoding damage — mojibake, replacement characters, truncation at a
    multi-byte boundary — shows up in no count and no sum.
    """
    assert target["unicode_sample"] == source["unicode_sample"]
    assert source["unicode_sample"], "the unicode fixtures are missing from the source"


def test_identity_sequences_at_high_water_mark(pg, target: dict) -> None:
    """
    The sequences are ready for the next real INSERT.

    Explicit primary keys were supplied for every migrated row, so the
    sequences still sit at 1 unless something reset them. Nothing warns
    you: it surfaces as a duplicate key error on the first write after
    go-live, which is the worst possible moment to discover it.
    """
    failures = []
    for table, col in [
        ("merchants", "merchant_id"),
        ("transactions", "txn_id"),
        ("batches", "batch_id"),
        ("settlements", "settlement_id"),
    ]:
        nextval, max_id = pg.execute(
            f"""SELECT last_value, (SELECT MAX({col}) FROM {table})
                FROM pg_sequences
                WHERE sequencename = pg_get_serial_sequence('{table}','{col}')
                                     ::regclass::text"""
        ).fetchone()
        if nextval is None or max_id is None:
            continue
        if nextval < max_id:
            failures.append(
                f"  {table}: sequence at {nextval:,} but max id is {max_id:,} "
                f"— the next insert collides"
            )

    assert not failures, "identity sequences not advanced:\n" + "\n".join(failures)


def test_generated_column_recomputed(pg) -> None:
    """
    The computed column produces the same answer it did on the source.

    SQL Server PERSISTED became GENERATED ALWAYS ... STORED, which means
    PostgreSQL calculates it rather than storing what was migrated. If
    the expression was transcribed wrong, this is where it shows.
    """
    wrong = pg.execute(
        """
        SELECT COUNT(*) FROM settlements
        WHERE gross_amount <> net_amount + fee_amount
        """
    ).fetchone()[0]
    assert wrong == 0, f"{wrong:,} settlements have an inconsistent generated column"


# ─────────────────────────────────────────────────────────────────────
#  Class 3 — the data is fine and the answers still changed
#
#  These do not fail. They assert a known, accepted semantic difference,
#  so that it is recorded rather than discovered in production.
#
#  Reloading cannot fix any of this. It needs a decision.
# ─────────────────────────────────────────────────────────────────────


def test_status_values_are_byte_identical(source: dict, target: dict) -> None:
    """
    Every distinct status value, and its count, survived exactly.

    This has to pass BEFORE the collation test below means anything. It
    proves the data is not the problem — so when a query returns fewer
    rows, the cause is semantics, not corruption.
    """
    assert target["status_counts"] == source["status_counts"]


def test_collation_semantics_changed_as_expected(source: dict, target: dict) -> None:
    """
    THE FINDING.

    SQL Server's default collation is case-insensitive. PostgreSQL is
    case-sensitive. The same WHERE clause therefore matches a different
    number of rows on each side, with no error and no warning.

    The previous test already established that every row and every value
    is intact. So this is not a migration defect — it is a change in what
    a query means, and no amount of reloading will alter it.

    Asserted rather than merely reported, so that a future change to the
    data or the collation makes this test fail loudly instead of quietly
    invalidating the finding.
    """
    src_ci = source["collation"]["case_insensitive"]
    src_cs = source["collation"]["case_sensitive"]
    tgt_cs = target["collation"]["case_sensitive"]
    tgt_ci = target["collation"]["case_insensitive"]

    # The insensitive count agrees across engines — the rows are all there.
    assert tgt_ci == src_ci

    # PostgreSQL's plain equality behaves like the case-sensitive count.
    assert tgt_cs == src_cs

    # And that is materially fewer rows than the legacy query returned.
    lost = src_ci - tgt_cs
    assert lost > 0, "expected the collation difference to be visible"

    pct = lost / src_ci * 100
    print(
        f"\n  Legacy query returned      {src_ci:>9,} rows"
        f"\n  Migrated query returns     {tgt_cs:>9,} rows"
        f"\n  Silently no longer matched {lost:>9,} rows  ({pct:.1f}%)"
        f"\n\n  The data is identical. The question changed."
        f"\n  Remediation is a decision, not a reload — see docs/FINDINGS.md"
    )


@pytest.mark.parametrize("term", ["Captured", "captured", "CAPTURED"])
def test_case_variants_all_present_in_data(pg, term: str) -> None:
    """
    Each case variant genuinely exists on the target.

    Proves the collation finding is about query semantics and not about
    one variant having been dropped or normalised during the load.
    """
    n = pg.execute(
        "SELECT COUNT(*) FROM transactions WHERE txn_status = %s", (term,)
    ).fetchone()[0]
    assert n > 0, f"no rows with status exactly {term!r} — was the data normalised?"


# ─────────────────────────────────────────────────────────────────────


def _same_instant(a: str | None, b: str | None) -> bool:
    """
    Compare two ISO timestamps as instants.

    The source is naive (DATETIME2 has no timezone) and the target is
    aware (timestamptz), so a string comparison would fail on formatting
    alone. Compare the moment, not the spelling.
    """
    from datetime import datetime, timezone

    if a is None or b is None:
        return a == b

    def parse(v: str):
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    try:
        return parse(a) == parse(b)
    except ValueError:
        return a == b


# ─────────────────────────────────────────────────────────────────────
#  Class 0 — the check that comparing two databases cannot make
# ─────────────────────────────────────────────────────────────────────


def test_source_database_matches_the_original_file() -> None:
    """
    The legacy database agrees with the file it was loaded from.

    This exists because of a real failure. BULK INSERT on Linux cannot
    read UTF-8, and dropping the unsupported CODEPAGE option loaded
    'σîùµû╣τë⌐µ╡ü' where '北方物流' belonged. Nothing errored.

    Thirteen of fourteen checks passed over that corrupted data, and the
    migration itself was flawless — it carried the corruption across
    faithfully, so source and target matched perfectly.

    That is the blind spot in every verification suite that only diffs
    two databases: it proves the move was faithful, never that the thing
    being moved was right. Somewhere the chain has to be anchored to
    something outside both systems.
    """
    from pathlib import Path

    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "migration"))
    from db import sqlserver_connection

    seed_file = Path(__file__).resolve().parent.parent / "sqlserver" / "data" / "merchants.psv"
    if not seed_file.exists():
        import pytest as _pytest

        _pytest.skip("seed file not present — run the generator first")

    expected = {}
    with seed_file.open(encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("|")
            if len(parts) > 2:
                expected[int(parts[0])] = parts[2]

    conn = sqlserver_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT merchant_id, legal_name FROM dbo.merchants")
        actual = {int(r[0]): r[1] for r in cur.fetchall()}
        cur.close()
    finally:
        conn.close()

    corrupted = [
        f"  merchant {mid}: file has {expected[mid]!r}, database has {actual[mid]!r}"
        for mid in sorted(expected)
        if mid in actual and actual[mid] != expected[mid]
    ]

    assert not corrupted, (
        "the legacy database does not match the file it was loaded from — "
        "the data was already wrong before any migration happened:\n"
        + "\n".join(corrupted[:10])
    )
