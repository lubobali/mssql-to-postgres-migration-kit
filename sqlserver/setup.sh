#!/usr/bin/env bash
#
# Stand up the legacy SQL Server, load the schema, and bulk-load the data.
# Run this ON the EC2 instance, reached via SSM Session Manager.
#
# Idempotent: safe to re-run. The container is recreated, the schema is
# dropped and rebuilt, and the data is reloaded from the same seeded
# generator — so the baseline numbers are reproducible.

set -euo pipefail

PROJECT="rds-migration-lab"
CONTAINER="legacy-sqlserver"
IMAGE="mcr.microsoft.com/mssql/server:2022-latest"
REGION="${AWS_REGION:-us-east-2}"

log() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

# ─── Credentials come from Secrets Manager, never from a file ────────

log "Reading the SA credential from Secrets Manager"
SA_PASSWORD="$(aws secretsmanager get-secret-value \
  --region "$REGION" \
  --secret-id "${PROJECT}/sqlserver/sa" \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"

if [[ -z "$SA_PASSWORD" ]]; then
  echo "Could not read the SA password. Is the instance profile attached?" >&2
  exit 1
fi

# ─── The container ───────────────────────────────────────────────────

log "Starting SQL Server 2022 (Developer Edition — free for non-production)"

# Remove the volume too, not just the container. A named volume survives
# docker rm, so the previous run's tables come back and the "idempotent"
# rebuild silently is not one — which is how a stale schema ends up
# being blamed on a migration bug.
sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
sudo docker volume rm mssql-data >/dev/null 2>&1 || true

sudo docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -e 'ACCEPT_EULA=Y' \
  -e "MSSQL_SA_PASSWORD=${SA_PASSWORD}" \
  -e 'MSSQL_PID=Developer' \
  -e 'MSSQL_COLLATION=SQL_Latin1_General_CP1_CI_AS' \
  -p 1433:1433 \
  -v mssql-data:/var/opt/mssql \
  "$IMAGE"

# NOTE on the collation above: CI_AS is case-INSENSITIVE, accent-sensitive.
# It is SQL Server's default and it is deliberate here — PostgreSQL is
# case-SENSITIVE, so this is the setting that makes the collation trap
# real rather than theoretical.

log "Waiting for SQL Server to accept connections"
for i in {1..60}; do
  if sudo docker exec "$CONTAINER" /opt/mssql-tools18/bin/sqlcmd \
      -S localhost -U sa -P "$SA_PASSWORD" -C -Q "SELECT 1" >/dev/null 2>&1; then
    echo "  ready after ${i}s"
    break
  fi
  if [[ $i -eq 60 ]]; then
    echo "SQL Server did not come up. Check: sudo docker logs $CONTAINER" >&2
    exit 1
  fi
  sleep 1
done

# ─── Schema ──────────────────────────────────────────────────────────

log "Creating the schema"
sudo docker cp 01_schema.sql "$CONTAINER":/tmp/01_schema.sql
sudo docker exec "$CONTAINER" /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "$SA_PASSWORD" -C -i /tmp/01_schema.sql

# ─── Data ────────────────────────────────────────────────────────────

log "Generating the dataset"
python3 generate_data.py --merchants 50 --transactions 250000

log "Bulk loading"
# BULK INSERT on Linux cannot read UTF-8 (no CODEPAGE option), and
# NVARCHAR is UTF-16 internally — so hand it UTF-16 and the encoding
# stops being a guess. The transactions file is pure ASCII and needs no
# conversion.
# -t UTF-16 (not UTF-16LE) because iconv only writes a byte order mark
# for the former, and BULK INSERT rejects widechar without one:
#   "DataFileType was incorrectly specified as widechar. DataFileType
#    will be assumed to be char because the data file does not have a
#    Unicode signature."
# It then falls back to char, mangles the encoding, and continues.
iconv -f UTF-8 -t UTF-16 data/merchants.psv > data/merchants.utf16.psv

sudo docker cp data/merchants.utf16.psv "$CONTAINER":/tmp/merchants.psv
sudo docker cp data/transactions.psv "$CONTAINER":/tmp/transactions.psv
sudo docker cp 02_load.sql           "$CONTAINER":/tmp/02_load.sql

sudo docker exec "$CONTAINER" /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "$SA_PASSWORD" -C -i /tmp/02_load.sql

log "Done"
echo "  Connect:  sudo docker exec -it $CONTAINER /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P '<pw>' -C -d payments"
