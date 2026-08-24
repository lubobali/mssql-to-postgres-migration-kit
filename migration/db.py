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

import boto3

PROJECT = os.environ.get("PROJECT", "rds-migration-lab")
REGION = os.environ.get("AWS_REGION", "us-east-2")


@functools.lru_cache(maxsize=None)
def _secret(name: str) -> dict:
    client = boto3.client("secretsmanager", region_name=REGION)
    raw = client.get_secret_value(SecretId=f"{PROJECT}/{name}")["SecretString"]
    return json.loads(raw)


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
