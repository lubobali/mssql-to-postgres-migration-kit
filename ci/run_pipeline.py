#!/usr/bin/env python3
"""
Build the source and run the migration, inside CI.

On the real environment this is three shell scripts driven by SSM. Here it
has to work on a bare runner with two service containers and no Docker
exec, so the same SQL runs through the drivers directly.

Deliberately the same SQL files. If CI drifted onto its own schema, a green
badge would stop meaning anything about the thing that actually ships.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "migration"))

from db import postgres_connection, sqlserver_connection  # noqa: E402


def split_batches(sql: str) -> list[str]:
    """
    Split on GO.

    GO is not T-SQL — it is a batch separator that sqlcmd understands and
    the ODBC driver does not. Sending a script containing GO straight to
    the driver fails on the first one.
    """
    parts = re.split(r"^\s*GO\s*$", sql, flags=re.MULTILINE | re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def run_sqlserver_script(path: Path, database: str | None = None) -> None:
    conn = sqlserver_connection(database) if database else sqlserver_connection("master")
    conn.autocommit = True
    cur = conn.cursor()
    for batch in split_batches(path.read_text(encoding="utf-8")):
        try:
            cur.execute(batch)
            while cur.nextset():
                pass
        except Exception as exc:  # noqa: BLE001
            print(f"  batch failed: {str(exc)[:200]}", file=sys.stderr)
            print(f"  {batch[:160]}", file=sys.stderr)
            raise
    cur.close()
    conn.close()


def main() -> None:
    pw = os.environ["SQLSERVER_PASSWORD"]

    print("▶ SQL Server schema")
    run_sqlserver_script(ROOT / "sqlserver" / "01_schema.sql")

    print("▶ Seeding")
    # BULK INSERT reads a path on the SERVER. In CI the server is a
    # container that cannot see the runner's filesystem, so the load goes
    # through the driver instead. The data is identical — it is generated
    # from the same seeded generator.
    _seed_via_driver(pw)

    print("▶ Deriving batches and settlements")
    _derive()

    print("▶ PostgreSQL schema")
    pg = postgres_connection()
    pg.autocommit = True
    pg.execute((ROOT / "postgres" / "01_schema.sql").read_text(encoding="utf-8"))
    pg.close()

    print("▶ Migrating")
    subprocess.run(
        [sys.executable, "migrate.py"],
        cwd=ROOT / "migration",
        check=True,
        env={**os.environ},
    )


def _seed_via_driver(pw: str) -> None:
    conn = sqlserver_connection("payments")
    conn.autocommit = False
    cur = conn.cursor()
    cur.fast_executemany = True

    data = ROOT / "sqlserver" / "data"

    def rows(name: str, cols: int):
        with (data / name).open(encoding="utf-8") as fh:
            for line in fh:
                f = line.rstrip("\n").split("|")
                yield [None if v == "" else v for v in f[:cols]]

    cur.execute("SET IDENTITY_INSERT dbo.merchants ON")
    cur.executemany(
        "INSERT INTO dbo.merchants (merchant_id, merchant_guid, legal_name, dba_name, "
        "mcc, is_active, onboarding_status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        list(rows("merchants.psv", 9)),
    )
    cur.execute("SET IDENTITY_INSERT dbo.merchants OFF")

    cur.execute("SET IDENTITY_INSERT dbo.transactions ON")
    batch: list[list] = []
    for r in rows("transactions.psv", 10):
        batch.append(r)
        if len(batch) >= 5000:
            cur.executemany(
                "INSERT INTO dbo.transactions (txn_id, merchant_id, amount, fee_amount, "
                "currency, card_last4, auth_code, txn_status, captured_at, settled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
            batch = []
    if batch:
        cur.executemany(
            "INSERT INTO dbo.transactions (txn_id, merchant_id, amount, fee_amount, "
            "currency, card_last4, auth_code, txn_status, captured_at, settled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
    cur.execute("SET IDENTITY_INSERT dbo.transactions OFF")

    conn.commit()
    cur.close()
    conn.close()


def _derive() -> None:
    conn = sqlserver_connection("payments")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET QUOTED_IDENTIFIER ON")
    cur.execute(
        """
        INSERT INTO dbo.batches (merchant_id, batch_date, txn_count, gross_amount, batch_status)
        SELECT t.merchant_id, CAST(t.captured_at AS DATE), COUNT(*), SUM(t.amount),
               CASE WHEN COUNT(t.settled_at) = COUNT(*) THEN 'Settled' ELSE 'Pending' END
        FROM dbo.transactions t
        WHERE t.txn_status = 'Captured'
        GROUP BY t.merchant_id, CAST(t.captured_at AS DATE)
        """
    )
    cur.execute(
        """
        INSERT INTO dbo.settlements (batch_id, net_amount, fee_amount, settled_at)
        SELECT b.batch_id, b.gross_amount * 0.9705, b.gross_amount * 0.0295,
               DATEADD(day, 2, CAST(b.batch_date AS DATETIME2(3)))
        FROM dbo.batches b WHERE b.batch_status = 'Settled'
        """
    )
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
