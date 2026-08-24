"""
Fixtures for the migration verification suite.

Both databases are profiled once per session, because profiling 250,000
rows twice per test would make the suite slow enough that people stop
running it — and a check nobody runs is not a check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "migration"))

from db import postgres_connection, sqlserver_connection  # noqa: E402
from profile_db import profile  # noqa: E402


@pytest.fixture(scope="session")
def source() -> dict:
    """The legacy SQL Server. This is the definition of truth."""
    conn = sqlserver_connection()
    try:
        return profile(conn, "sqlserver")
    finally:
        conn.close()


@pytest.fixture(scope="session")
def target() -> dict:
    """The migrated PostgreSQL database."""
    conn = postgres_connection()
    try:
        return profile(conn, "postgres")
    finally:
        conn.close()


@pytest.fixture(scope="session")
def pg():
    """A live PostgreSQL connection, for checks that need to query directly."""
    conn = postgres_connection()
    yield conn
    conn.close()
