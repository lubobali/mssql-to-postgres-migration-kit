#!/usr/bin/env python3
"""
Export to S3 as Parquet, partitioned by date — the Snowflake path.

The job description asks for "data pipeline automation to Snowflake".
This is the shape of it, and it needs no Snowflake account to be real:
Snowflake reads Parquet from S3 natively through an external stage, so
the deliverable is the files and the layout, not the warehouse.

Three decisions worth stating:

  PARQUET, NOT CSV     Columnar and compressed. A settlement query reads
                       two columns out of ten and touches a fraction of
                       the bytes. CSV also loses types — every value
                       arrives as text and something downstream has to
                       guess, which is how a numeric(19,4) becomes a
                       float and cents disappear.

  PARTITIONED BY DATE  Snowflake and Athena prune whole partitions from
                       the path. Querying one day should not scan a year.

  numeric AS DECIMAL   Explicitly. The default Arrow inference for a
                       Postgres numeric can land on float64, which
                       reintroduces the exact error the whole project
                       exists to detect. Money stays exact end to end or
                       the pipeline is not trustworthy.

This is the analytics path, and it is a different problem from the
transactional migration. Both appear in the job description and they are
not the same job: RDS answers thousands of small reads and writes per
second; this answers "sum a quarter".
"""

from __future__ import annotations

import argparse
import io
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "migration"))

import boto3  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from db import postgres_connection  # noqa: E402

REGION = "us-east-2"

# Money as DECIMAL, not float. The single most important line in the file.
SCHEMA = pa.schema(
    [
        ("txn_id", pa.int64()),
        ("merchant_id", pa.int32()),
        ("amount", pa.decimal128(19, 4)),
        ("fee_amount", pa.decimal128(19, 4)),
        ("currency", pa.string()),
        ("txn_status", pa.string()),
        ("captured_at", pa.timestamp("us", tz="UTC")),
        ("settled_at", pa.timestamp("us", tz="UTC")),
    ]
)

# The date arithmetic belongs in SQL, not in a Python f-string. Passing
# "2026-11-01::date + 1" as a PARAMETER sends that text to Postgres as a
# value, which fails with "invalid input syntax for type timestamp with
# time zone". A parameter is a value; it is never fragment of SQL.
QUERY = """
SELECT txn_id, merchant_id, amount, fee_amount, currency,
       txn_status, captured_at, settled_at
FROM transactions
WHERE captured_at >= %(day)s::date
  AND captured_at <  %(day)s::date + interval '1 day'
ORDER BY txn_id
"""


def export_day(conn, bucket: str, day: str, prefix: str) -> tuple[int, int]:
    rows = conn.execute(QUERY, {"day": day}).fetchall()
    if not rows:
        return 0, 0

    cols = list(zip(*rows))
    table = pa.Table.from_arrays(
        [
            pa.array(cols[0], type=pa.int64()),
            pa.array(cols[1], type=pa.int32()),
            pa.array([Decimal(str(v)) for v in cols[2]], type=pa.decimal128(19, 4)),
            pa.array([Decimal(str(v)) for v in cols[3]], type=pa.decimal128(19, 4)),
            pa.array([str(v).strip() for v in cols[4]], type=pa.string()),
            pa.array(cols[5], type=pa.string()),
            pa.array(cols[6], type=pa.timestamp("us", tz="UTC")),
            pa.array(cols[7], type=pa.timestamp("us", tz="UTC")),
        ],
        schema=SCHEMA,
    )

    buf = io.BytesIO()
    # Snappy: the default for a reason. zstd compresses harder but Athena
    # and older Snowflake readers are less consistent about it, and this
    # is a file format chosen to be read by things you do not control.
    pq.write_table(table, buf, compression="snappy")
    body = buf.getvalue()

    # Hive-style partitioning. Snowflake and Athena both prune on this
    # path structure, so one day's query reads one day's bytes.
    key = f"{prefix}/capture_date={day}/transactions-{day}.parquet"
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=bucket, Key=key, Body=body
    )
    return len(rows), len(body)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", default="payments/transactions")
    p.add_argument("--days", type=int, default=7)
    a = p.parse_args()

    conn = postgres_connection()
    conn.autocommit = True

    days = [
        r[0].isoformat()
        for r in conn.execute(
            """
            SELECT DISTINCT captured_at::date AS d
            FROM transactions
            ORDER BY d DESC
            LIMIT %s
            """,
            (a.days,),
        ).fetchall()
    ]

    total_rows = total_bytes = 0
    for day in days:
        n, size = export_day(conn, a.bucket, day, a.prefix)
        total_rows += n
        total_bytes += size
        print(f"  {day}  {n:>8,} rows  {size / 1024:>8.0f} KB")

    conn.close()

    print(f"\n  {total_rows:,} rows in {total_bytes / 1024 / 1024:.1f} MB Parquet")
    print(f"  s3://{a.bucket}/{a.prefix}/capture_date=.../")
    print(
        "\n  Snowflake reads this directly:\n"
        "\n    CREATE OR REPLACE STAGE payments_stage"
        f"\n      URL = 's3://{a.bucket}/{a.prefix}/'"
        "\n      STORAGE_INTEGRATION = payments_s3_int"
        "\n      FILE_FORMAT = (TYPE = PARQUET);"
        "\n"
        "\n    CREATE OR REPLACE EXTERNAL TABLE transactions_ext"
        "\n      PARTITION BY (capture_date)"
        "\n      LOCATION = @payments_stage"
        "\n      AUTO_REFRESH = TRUE"
        "\n      FILE_FORMAT = (TYPE = PARQUET);"
        "\n"
        "\n  STORAGE_INTEGRATION, not keys in a URL — Snowflake assumes an"
        "\n  IAM role, so nothing has a long-lived credential."
    )


if __name__ == "__main__":
    main()
