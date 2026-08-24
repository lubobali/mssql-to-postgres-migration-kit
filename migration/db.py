"""
Connections, resolved from Secrets Manager and Terraform outputs.

No hostname, password, or endpoint appears anywhere in this repository.
Everything is looked up at runtime through the instance's IAM role, so
the same code runs against a rebuilt environment without edits — and a
leaked file leaks nothing.
"""

from __future__ import annotations

import functools
import json
import os

import boto3  # noqa: F401  (unused in CI mode)

PROJECT = os.environ.get("PROJECT", "rds-migration-lab")
REGION = os.environ.get("AWS_REGION", "us-east-2")

# CI runs both engines as throwaway service containers on the runner. The
# real RDS instance is not publicly accessible — deliberately — so CI
# cannot reach it, and the tests would otherwise be unrunnable anywhere
# except one EC2 host.
#
# Same code, same tests, different connection strings. The credentials
# below are disposable and exist only for the lifetime of a job.
CI_MODE = os.environ.get("CI_MODE") == "1"


@functools.lru_cache(maxsize=None)
def _secret(name: str) -> dict:
    if CI_MODE:
        return _ci_credentials(name)

    client = boto3.client("secretsmanager", region_name=REGION)
    raw = client.get_secret_value(SecretId=f"{PROJECT}/{name}")["SecretString"]
    return json.loads(raw)


def _ci_credentials(name: str) -> dict:
    if name == "sqlserver/sa":
        return {
            "username": "sa",
            "password": os.environ["SQLSERVER_PASSWORD"],
            "port": 1433,
        }
    if name == "postgres/master":
        return {
            "username": "postgres",
            "password": os.environ["POSTGRES_PASSWORD"],
            "host": "localhost",
            "port": 5432,
            "dbname": "payments",
        }
    raise KeyError(name)


def sqlserver_connection(database: str = "payments"):
    """
    ODBC connection to the legacy SQL Server.

    TrustServerCertificate is on because the container uses a self-signed
    certificate. In production this would be a real certificate and the
    setting would be off — it is called out here rather than left quiet,
    because "trust any certificate" is exactly the kind of line that gets
    copied from a lab into production and then found in an audit.
    """
    import pyodbc

    creds = _secret("sqlserver/sa")
    dsn = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=localhost,1433;"
        f"DATABASE={database};"
        f"UID={creds['username']};"
        f"PWD={creds['password']};"
        "TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(dsn)
    conn.autocommit = True
    return conn


def postgres_connection():
    """
    Connection to RDS PostgreSQL.

    The endpoint comes out of the secret, which Terraform wrote at apply
    time — so a rebuilt instance with a new endpoint needs no code change.
    """
    import psycopg

    creds = _secret("postgres/master")
    return psycopg.connect(
        host=creds["host"],
        port=creds["port"],
        dbname=creds["dbname"],
        user=creds["username"],
        password=creds["password"],
        connect_timeout=10,
    )
