#!/usr/bin/env python3
"""
Backup and restore drill, timed.

The point is not that RDS can take a snapshot. It is that a number comes
out of this, and an SLA made of anything other than a measured number is
a wish.

  1. Snapshot the production instance and time it
  2. Restore the snapshot to a NEW instance and time that
  3. Run the verification suite against the restored copy
  4. Delete the restored instance

Step 3 is the part usually skipped, and it is the only part that turns a
backup into a proven backup. A snapshot that restores into a database
nobody queried is a file, not a recovery plan.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import boto3

PROJECT = "rds-migration-lab"
SOURCE_DB = f"{PROJECT}-pg"
RESTORE_DB = f"{PROJECT}-restoretest"
REGION = "us-east-2"

rds = boto3.client("rds", region_name=REGION)


def log(msg: str) -> None:
    print(f"\n\033[1m▶ {msg}\033[0m", flush=True)


def wait(waiter_name: str, **kwargs) -> float:
    """Wait, and return how long it took."""
    started = time.monotonic()
    waiter = rds.get_waiter(waiter_name)
    waiter.wait(**kwargs, WaiterConfig={"Delay": 15, "MaxAttempts": 120})
    return time.monotonic() - started


def snapshot(tag: str) -> tuple[str, float]:
    snap_id = f"{PROJECT}-drill-{tag}"
    log(f"Snapshotting {SOURCE_DB} -> {snap_id}")

    try:
        rds.delete_db_snapshot(DBSnapshotIdentifier=snap_id)
        time.sleep(5)
    except rds.exceptions.DBSnapshotNotFoundFault:
        pass

    rds.create_db_snapshot(
        DBSnapshotIdentifier=snap_id,
        DBInstanceIdentifier=SOURCE_DB,
        Tags=[{"Key": "project", "Value": PROJECT}],
    )
    secs = wait("db_snapshot_completed", DBSnapshotIdentifier=snap_id)
    print(f"  completed in {secs:.0f}s")
    return snap_id, secs


def restore(snap_id: str) -> float:
    log(f"Restoring {snap_id} -> {RESTORE_DB}")

    try:
        rds.delete_db_instance(
            DBInstanceIdentifier=RESTORE_DB,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=True,
        )
        print("  an old restore instance existed; waiting for it to go")
        wait("db_instance_deleted", DBInstanceIdentifier=RESTORE_DB)
    except rds.exceptions.DBInstanceNotFoundFault:
        pass

    src = rds.describe_db_instances(DBInstanceIdentifier=SOURCE_DB)["DBInstances"][0]

    rds.restore_db_instance_from_db_snapshot(
        DBInstanceIdentifier=RESTORE_DB,
        DBSnapshotIdentifier=snap_id,
        DBInstanceClass=src["DBInstanceClass"],
        DBSubnetGroupName=src["DBSubnetGroup"]["DBSubnetGroupName"],
        VpcSecurityGroupIds=[g["VpcSecurityGroupId"] for g in src["VpcSecurityGroups"]],
        # Same parameter group, or the restored copy is not a faithful
        # copy — different settings mean a different database.
        DBParameterGroupName=src["DBParameterGroups"][0]["DBParameterGroupName"],
        PubliclyAccessible=False,
        MultiAZ=False,
        Tags=[{"Key": "project", "Value": PROJECT}],
    )
    secs = wait("db_instance_available", DBInstanceIdentifier=RESTORE_DB)
    print(f"  available in {secs:.0f}s")
    return secs


def verify_restored() -> tuple[bool, dict]:
    """Query the restored copy and compare it to the live one."""
    log("Verifying the restored copy")

    inst = rds.describe_db_instances(DBInstanceIdentifier=RESTORE_DB)["DBInstances"][0]
    host = inst["Endpoint"]["Address"]

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "migration"))
    import psycopg
    from db import _secret, postgres_connection
    from profile_db import profile

    creds = _secret("postgres/master")

    # The master credential is restored with the snapshot, so the same
    # password works against the restored endpoint.
    restored = psycopg.connect(
        host=host,
        port=creds["port"],
        dbname=creds["dbname"],
        user=creds["username"],
        password=creds["password"],
        connect_timeout=15,
    )
    try:
        restored_profile = profile(restored, "postgres")
    finally:
        restored.close()

    live = postgres_connection()
    try:
        live_profile = profile(live, "postgres")
    finally:
        live.close()

    same_rows = restored_profile["row_counts"] == live_profile["row_counts"]
    same_money = restored_profile["money_sums"] == live_profile["money_sums"]

    for table, n in restored_profile["row_counts"].items():
        print(f"  {table:<14} {n:>10,}")
    print(f"\n  row counts match  {same_rows}")
    print(f"  money sums match  {same_money}")

    return (same_rows and same_money), restored_profile


def teardown() -> None:
    log(f"Deleting {RESTORE_DB}")
    try:
        rds.delete_db_instance(
            DBInstanceIdentifier=RESTORE_DB,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=True,
        )
        print("  deletion started (runs in the background)")
    except rds.exceptions.DBInstanceNotFoundFault:
        print("  already gone")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keep", action="store_true", help="do not delete the restored instance")
    a = p.parse_args()

    tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    total = time.monotonic()

    snap_id, snap_secs = snapshot(tag)
    restore_secs = restore(snap_id)
    ok, _ = verify_restored()

    if not a.keep:
        teardown()

    elapsed = time.monotonic() - total

    result = {
        "snapshot_seconds": round(snap_secs),
        "restore_seconds": round(restore_secs),
        "verification_passed": ok,
        "total_seconds": round(elapsed),
        "snapshot_id": snap_id,
    }

    log("Result")
    print(json.dumps(result, indent=2))
    print(
        f"\n  Recovery time for this dataset: about {round(restore_secs / 60)} minutes"
        f"\n  from an existing snapshot, on a db.t4g.micro."
        f"\n\n  That number is what an RTO is made of. Anything else is a guess."
    )

    with open("restore_drill_result.json", "w") as fh:
        json.dump(result, fh, indent=2)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
