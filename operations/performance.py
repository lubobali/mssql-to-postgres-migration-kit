#!/usr/bin/env python3
"""
Performance after migration: measure, change one thing, measure again.

The interesting case here is not a missing index. It is the query that
was fast on SQL Server because of something PostgreSQL does not have.

The transactions table was CLUSTERED on captured_at. In SQL Server that
means the rows are physically ordered by capture date and stay that way,
so a settlement report scanning a date range reads sequential pages.
PostgreSQL has no maintained clustered index — CLUSTER is a one-time
reorder that decays — so the same query has different physics on the
other side.

That is a migration finding, not a tuning exercise, and it is invisible
in any row count or checksum.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "migration"))

from db import postgres_connection  # noqa: E402

# The nightly settlement query, which is the one that runs every day and
# therefore the only one whose plan is worth arguing about.
SETTLEMENT_RANGE = """
SELECT t.merchant_id,
       COUNT(*)        AS txn_count,
       SUM(t.amount)   AS gross
FROM transactions t
WHERE t.captured_at >= %s AND t.captured_at < %s
  AND lower(t.txn_status) = 'captured'
GROUP BY t.merchant_id
"""

WINDOW = ("2026-03-01", "2026-04-01")


def explain(conn, sql: str, params: tuple) -> dict:
    cur = conn.cursor()
    cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params)
    plan = cur.fetchone()[0][0]
    cur.close()
    return plan


def timed(conn, sql: str, params: tuple, runs: int = 5) -> tuple[float, float]:
    """Median and best of N runs. A single timing is noise."""
    times = []
    cur = conn.cursor()
    for _ in range(runs):
        started = time.perf_counter()
        cur.execute(sql, params)
        cur.fetchall()
        times.append((time.perf_counter() - started) * 1000)
    cur.close()
    return statistics.median(times), min(times)


def summarise(plan: dict) -> dict:
    node = plan["Plan"]

    def walk(n, out):
        out.append(n["Node Type"])
        for child in n.get("Plans", []):
            walk(child, out)
        return out

    return {
        "node_types": walk(node, []),
        "execution_ms": round(plan["Execution Time"], 2),
        "planning_ms": round(plan["Planning Time"], 2),
        "shared_read": node.get("Shared Read Blocks", 0),
        "shared_hit": node.get("Shared Hit Blocks", 0),
        "rows": node.get("Actual Rows"),
    }


def main() -> None:
    conn = postgres_connection()
    conn.autocommit = True
    results: dict = {}

    print("\033[1m▶ Baseline — as migrated\033[0m")
    print("  (btree on captured_at; the clustered index did not survive)")

    conn.execute("DROP INDEX IF EXISTS ix_txn_settlement_covering")
    conn.execute("ANALYZE transactions")

    before_plan = summarise(explain(conn, SETTLEMENT_RANGE, WINDOW))
    before_med, before_best = timed(conn, SETTLEMENT_RANGE, WINDOW)
    results["before"] = {**before_plan, "median_ms": round(before_med, 2)}

    print(f"  plan          {' -> '.join(before_plan['node_types'][:4])}")
    print(f"  rows          {before_plan['rows']:,}")
    print(f"  buffers read  {before_plan['shared_read']:,}")
    print(f"  median        {before_med:.1f} ms   (best {before_best:.1f} ms)")

    print("\n\033[1m▶ Adding a covering index\033[0m")
    print("  lower(txn_status) is not sargable against a plain btree on")
    print("  txn_status, so the status filter cannot use an index at all.")
    print("  This one is functional on lower(), leads on captured_at for")
    print("  the range, and INCLUDEs the aggregated columns so the heap")
    print("  does not have to be visited.")

    started = time.monotonic()
    conn.execute(
        """
        CREATE INDEX ix_txn_settlement_covering
            ON transactions (captured_at, lower(txn_status))
            INCLUDE (merchant_id, amount)
        """
    )
    build_secs = time.monotonic() - started
    conn.execute("ANALYZE transactions")
    print(f"  built in {build_secs:.1f}s")

    after_plan = summarise(explain(conn, SETTLEMENT_RANGE, WINDOW))
    after_med, after_best = timed(conn, SETTLEMENT_RANGE, WINDOW)
    results["after"] = {**after_plan, "median_ms": round(after_med, 2)}

    print(f"\n  plan          {' -> '.join(after_plan['node_types'][:4])}")
    print(f"  rows          {after_plan['rows']:,}")
    print(f"  buffers read  {after_plan['shared_read']:,}")
    print(f"  median        {after_med:.1f} ms   (best {after_best:.1f} ms)")

    speedup = before_med / after_med if after_med else 0
    results["speedup"] = round(speedup, 2)
    results["index_build_seconds"] = round(build_secs, 1)

    print("\n\033[1m▶ Result\033[0m")
    print(f"  {before_med:.1f} ms  ->  {after_med:.1f} ms   ({speedup:.1f}x)")

    # ─── index size, because it is not free ──────────────────────────
    size = conn.execute(
        "SELECT pg_size_pretty(pg_relation_size('ix_txn_settlement_covering'))"
    ).fetchone()[0]
    table_size = conn.execute(
        "SELECT pg_size_pretty(pg_relation_size('transactions'))"
    ).fetchone()[0]
    results["index_size"] = size
    results["table_size"] = table_size
    print(f"  index {size} against a {table_size} table")
    print("  Every index is paid for on every write. Worth saying out loud")
    print("  on a table taking payment traffic.")

    # ─── what pg_stat_statements saw ─────────────────────────────────
    print("\n\033[1m▶ Top statements by mean time\033[0m")
    try:
        rows = conn.execute(
            """
            SELECT calls,
                   round(mean_exec_time::numeric, 2),
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 70)
            FROM pg_stat_statements
            WHERE query NOT ILIKE '%pg_stat_statements%'
            ORDER BY mean_exec_time DESC LIMIT 5
            """
        ).fetchall()
        for calls, ms, q in rows:
            print(f"  {calls:>6} calls  {ms:>9} ms  {q}")
    except Exception as exc:  # noqa: BLE001
        print(f"  unavailable: {str(exc)[:80]}")

    conn.close()

    Path("performance_result.json").write_text(json.dumps(results, indent=2))
    print("\nwrote performance_result.json")


if __name__ == "__main__":
    main()
