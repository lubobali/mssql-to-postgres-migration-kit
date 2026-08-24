#!/usr/bin/env python3
"""
Drive the DMS task, and prove CDC actually works.

The full load is the boring half. Any tool can copy a table.

The half that matters is what happens after: a row written to the source
AFTER the copy finished still arrives on the target, with no second run
and no downtime. That is what lets the source stay live during a
migration, and it is the entire reason a real cutover uses DMS rather
than a script.

So this does not just start the task. It waits for full load, then writes
to SQL Server and watches the row appear in PostgreSQL.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from db import postgres_connection, sqlserver_connection

REGION = "us-east-2"
PROJECT = "rds-migration-lab"

dms = boto3.client("dms", region_name=REGION)


def log(msg: str) -> None:
    print(f"\n\033[1m▶ {msg}\033[0m", flush=True)


def task_arn() -> str:
    tasks = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-id", "Values": [f"{PROJECT}-task"]}]
    )["ReplicationTasks"]
    if not tasks:
        sys.exit("task not found — has terraform applied?")
    return tasks[0]["ReplicationTaskArn"]


def endpoint_arns() -> tuple[str, str]:
    eps = {e["EndpointIdentifier"]: e["EndpointArn"] for e in dms.describe_endpoints()["Endpoints"]}
    return eps[f"{PROJECT}-source-mssql"], eps[f"{PROJECT}-target-pg"]


def instance_arn() -> str:
    return dms.describe_replication_instances(
        Filters=[{"Name": "replication-instance-id", "Values": [f"{PROJECT}-dms"]}]
    )["ReplicationInstances"][0]["ReplicationInstanceArn"]


def test_endpoints() -> bool:
    """
    Test both connections before starting anything.

    A task that starts against an unreachable endpoint fails minutes
    later with a message about the task rather than about the network,
    which is a bad way to spend an afternoon.
    """
    log("Testing endpoint connections")
    inst = instance_arn()
    ok = True

    for arn in endpoint_arns():
        ident = arn.split(":")[-1]
        try:
            dms.test_connection(ReplicationInstanceArn=inst, EndpointArn=arn)
        except dms.exceptions.InvalidResourceStateFault:
            pass  # a test is already running for this pair

        for _ in range(40):
            conns = dms.describe_connections(
                Filters=[{"Name": "endpoint-arn", "Values": [arn]}]
            )["Connections"]
            status = conns[0]["Status"] if conns else "testing"
            if status in ("successful", "failed"):
                break
            time.sleep(5)

        mark = "ok" if status == "successful" else "FAILED"
        print(f"  {ident:<40} {mark}")
        if status != "successful":
            ok = False
            if conns and conns[0].get("LastFailureMessage"):
                print(f"    {conns[0]['LastFailureMessage'][:180]}")

    return ok


def start_task(arn: str) -> None:
    state = dms.describe_replication_tasks(
        Filters=[{"Name": "replication-task-arn", "Values": [arn]}]
    )["ReplicationTasks"][0]["Status"]

    log(f"Starting the task (currently {state})")

    start_type = "reload-target" if state in ("stopped", "failed", "ready") else "resume-processing"
    dms.start_replication_task(ReplicationTaskArn=arn, StartReplicationTaskType=start_type)


def wait_for_full_load(arn: str, timeout: int = 1800) -> dict:
    log("Full load")
    started = time.monotonic()
    last = ""

    while time.monotonic() - started < timeout:
        t = dms.describe_replication_tasks(
            Filters=[{"Name": "replication-task-arn", "Values": [arn]}]
        )["ReplicationTasks"][0]

        stats = t.get("ReplicationTaskStats", {})
        pct = stats.get("FullLoadProgressPercent", 0)
        status = t["Status"]

        line = f"  {status:<24} {pct:>3}%  tables loaded {stats.get('TablesLoaded', 0)}"
        if line != last:
            print(line, flush=True)
            last = line

        if status == "failed":
            print(f"  {t.get('LastFailureMessage', '')[:300]}")
            sys.exit(1)

        # running + 100% means the full load finished and CDC has begun
        if status == "running" and pct == 100:
            secs = time.monotonic() - started
            print(f"\n  full load complete in {secs:.0f}s — CDC is now applying changes")
            return {"seconds": round(secs), "stats": stats}

        time.sleep(10)

    sys.exit("timed out waiting for full load")


def prove_cdc(arn: str) -> bool:
    """
    Write to the source AFTER the full load, and watch it arrive.

    This is the claim being tested. Everything before it is a file copy.
    """
    log("Proving CDC — writing to the source after the load finished")

    marker = int(time.time())
    amount = Decimal(f"{marker % 9999}.4321")

    src = sqlserver_connection()
    src.autocommit = True
    cur = src.cursor()
    cur.execute(
        """
        INSERT INTO dbo.transactions
            (merchant_id, amount, fee_amount, currency, card_last4,
             auth_code, txn_status, captured_at)
        VALUES (1, ?, 0.0001, 'USD', '4321', ?, 'CDCTEST', SYSUTCDATETIME())
        """,
        (amount, str(marker)[-6:]),
    )
    cur.execute("SELECT MAX(txn_id) FROM dbo.transactions WHERE txn_status = 'CDCTEST'")
    src_id = cur.fetchone()[0]
    cur.close()
    src.close()

    print(f"  inserted txn_id {src_id} on SQL Server at {datetime.now(timezone.utc):%H:%M:%S}Z")
    print("  watching PostgreSQL...")

    pg = postgres_connection()
    pg.autocommit = True
    started = time.monotonic()

    try:
        while time.monotonic() - started < 300:
            row = pg.execute(
                "SELECT txn_id, amount, txn_status FROM transactions WHERE txn_id = %s",
                (src_id,),
            ).fetchone()
            if row:
                lag = time.monotonic() - started
                print(f"\n  arrived after {lag:.1f}s")
                print(f"  txn_id {row[0]}   amount {row[1]}   status {row[2]}")
                if Decimal(str(row[1])) != amount:
                    print(f"  MISMATCH: source had {amount}")
                    return False
                print("\n  The source never stopped accepting writes.")
                print("  That is the difference between DMS and a copy script.")
                return True
            time.sleep(3)
    finally:
        pg.close()

    print("\n  did not arrive within 300s")
    return False


def table_stats(arn: str) -> None:
    log("Per-table statistics")
    stats = dms.describe_table_statistics(ReplicationTaskArn=arn)["TableStatistics"]
    print(f"  {'table':<16}{'full load':>12}{'inserts':>10}{'validation':>26}")
    for t in sorted(stats, key=lambda s: s["TableName"]):
        print(
            f"  {t['TableName']:<16}"
            f"{t.get('FullLoadRows', 0):>12,}"
            f"{t.get('Inserts', 0):>10,}"
            f"{t.get('ValidationState', 'n/a'):>26}"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-test", action="store_true")
    a = p.parse_args()

    arn = task_arn()

    if not a.skip_test and not test_endpoints():
        sys.exit("endpoint test failed — fix connectivity before starting the task")

    start_task(arn)
    result = wait_for_full_load(arn)

    # DMS validation runs after the load and compares row by row. It is
    # a second opinion, not a replacement for the suite: it checks that
    # DMS moved what DMS read, and knows nothing about what the values
    # are supposed to mean.
    print("\n  waiting for DMS row-level validation")
    time.sleep(60)

    table_stats(arn)
    ok = prove_cdc(arn)
    table_stats(arn)

    log("Result")
    print(f"  full load     {result['seconds']}s")
    print(f"  CDC proven    {ok}")
    print("\n  Stop the task when finished, or it keeps replicating:")
    print(f"    aws dms stop-replication-task --replication-task-arn {arn}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
