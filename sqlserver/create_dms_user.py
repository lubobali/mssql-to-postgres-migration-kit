#!/usr/bin/env python3
"""
Create the DMS login.

This was originally sqlcmd with -v DMS_PASSWORD and $(DMS_PASSWORD) in
the script. It failed silently: the login was created with the wrong
password and the only symptom was DMS reporting "Login failed for user
'dms_user'" — a message that points at permissions, not at substitution.

sqlcmd variable substitution inside dynamic SQL is one indirection too
many. CREATE LOGIN cannot take a bound parameter, so the password has to
be inlined into the statement somewhere; doing it here means it can be
verified immediately afterwards instead of hoped about.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "migration"))

import pyodbc  # noqa: E402

from db import _secret  # noqa: E402


def main() -> None:
    sa = _secret("sqlserver/sa")
    dms = _secret("sqlserver/dms")
    pw = dms["password"]

    # CREATE LOGIN takes no bound parameters, so this string is built by
    # concatenation. Assert the value cannot break out of the literal
    # rather than trusting the generator to keep its promises — the
    # generator lives in a different file and could change.
    if "'" in pw or ";" in pw or "--" in pw:
        sys.exit("password contains a character that could escape the literal")

    dsn = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=localhost,1433;DATABASE=master;"
        f"UID={sa['username']};PWD={sa['password']};"
        "TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(dsn, autocommit=True)
    cur = conn.cursor()

    exists = cur.execute(
        "SELECT COUNT(*) FROM sys.server_principals WHERE name = 'dms_user'"
    ).fetchone()[0]

    if exists:
        cur.execute(f"ALTER LOGIN dms_user WITH PASSWORD = '{pw}'")
        print("  login existed; password rotated")
    else:
        cur.execute(f"CREATE LOGIN dms_user WITH PASSWORD = '{pw}', CHECK_POLICY = OFF")
        print("  login created")

    # VIEW SERVER STATE: how DMS reads sys.dm_* to find the log position.
    cur.execute("GRANT VIEW SERVER STATE TO dms_user")

    # msdb: DMS checks the CDC capture job is actually running. Without
    # this the task starts and then reads nothing, quietly.
    cur.execute("USE msdb")
    cur.execute(
        "IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'dms_user') "
        "CREATE USER dms_user FOR LOGIN dms_user"
    )
    for t in ("sysjobs", "sysjobsteps", "sysjobhistory"):
        cur.execute(f"GRANT SELECT ON msdb.dbo.{t} TO dms_user")

    # The source database. db_owner is what AWS documents for MS-CDC and
    # is more than strictly required — DMS reads cdc.* and the source
    # tables, it does not alter schema. Narrowing it is possible and is
    # what a PCI audit would ask about.
    cur.execute("USE payments")
    cur.execute(
        "IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'dms_user') "
        "CREATE USER dms_user FOR LOGIN dms_user"
    )
    cur.execute("ALTER ROLE db_owner ADD MEMBER dms_user")

    # DMS's own row validator reads the transaction log through
    # sys.fn_dblog, which is not covered by db_owner or VIEW SERVER
    # STATE. Without it the capture works and the VALIDATOR component
    # fails, which reads as a task failure:
    #   "The SELECT permission was denied on the object 'fn_dblog',
    #    database 'mssqlsystemresource', schema 'sys'."
    cur.execute("USE master")
    for stmt in (
        "GRANT SELECT ON sys.fn_dblog TO dms_user",
        "GRANT VIEW ANY DEFINITION TO dms_user",
        "GRANT VIEW DATABASE STATE TO dms_user",
    ):
        try:
            cur.execute(stmt)
            print(f"  granted: {stmt.split(' ON ')[-1].split(' TO ')[0] if ' ON ' in stmt else stmt[6:].split(' TO ')[0]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  could not grant ({stmt[:40]}...): {str(exc)[:90]}")

    is_sa = cur.execute("SELECT IS_SRVROLEMEMBER('sysadmin', 'dms_user')").fetchone()[0]
    cur.close()
    conn.close()

    # ─── Verify, rather than assume ──────────────────────────────────
    #
    # The original failure was a login created with a password nobody
    # checked. Connecting as the new user immediately is the whole
    # difference between "created" and "works".
    verify = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=localhost,1433;DATABASE=payments;"
        f"UID=dms_user;PWD={pw};TrustServerCertificate=yes;"
    )
    try:
        c2 = pyodbc.connect(verify, timeout=10)
        who = c2.cursor().execute("SELECT SUSER_NAME()").fetchone()[0]
        c2.close()
        print(f"  verified: connected as {who}")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"  login does not work: {str(exc)[:160]}")

    if is_sa:
        sys.exit("  dms_user IS sysadmin — DMS will choose MS-REPLICATION and fail")
    print("  sysadmin: no  ->  DMS will use MS-CDC")


if __name__ == "__main__":
    main()
