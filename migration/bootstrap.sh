#!/usr/bin/env bash
#
# Install the database clients the migration and verification need.
#
# Shelling out to sqlcmd works for a one-off query but falls apart the
# moment you need typed results — and a verification suite comparing
# numeric(19,4) sums cannot afford to parse numbers back out of console
# text. So: a real ODBC driver and real Python drivers.

set -euo pipefail

log() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

log "Microsoft ODBC Driver 18 for SQL Server"

# Microsoft does not publish an Amazon Linux 2023 repo. RHEL 9 is the
# supported equivalent and is what AL2023 is built against.
sudo curl -sSL -o /etc/yum.repos.d/mssql-release.repo \
  https://packages.microsoft.com/config/rhel/9/prod.repo

sudo ACCEPT_EULA=Y dnf install -y -q msodbcsql18 unixODBC-devel

log "Build tooling and PostgreSQL headers"
sudo dnf install -y -q gcc python3-devel libpq-devel

log "Python drivers"
python3 -m pip install --quiet --user \
  'pyodbc>=5.1' \
  'psycopg[binary]>=3.1' \
  'boto3>=1.34' \
  'pytest>=8.0' \
  'tabulate>=0.9' \
  'pyarrow>=15.0'

log "Checking"
python3 - <<'PY'
import pyodbc, psycopg, boto3
print(f"  pyodbc   {pyodbc.version}")
print(f"  psycopg  {psycopg.__version__}")
print(f"  boto3    {boto3.__version__}")
drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
print(f"  ODBC     {drivers or 'NONE FOUND — the driver did not install'}")
PY
