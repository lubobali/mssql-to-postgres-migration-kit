# Runbook

Build it from nothing, and take it apart.

Every command here was run. The gotchas in [Troubleshooting](#troubleshooting) are the ones
that actually happened, not the ones that might.

> **The fastest proof it works is [the CI run](https://github.com/lubobali/mssql-to-postgres-migration-kit/actions/workflows/ci.yml).**
> It builds SQL Server 2022 and PostgreSQL 15 from scratch on a clean runner, seeds them,
> migrates, and runs all 15 checks. No AWS account required to watch it happen.

---

## Prerequisites

```bash
brew install awscli terraform
brew install --cask session-manager-plugin    # for a shell on the EC2 host
aws configure                                  # region us-east-2, output json
```

The AWS identity needs to create VPC, EC2, RDS, DMS, IAM, Secrets Manager, S3, CloudWatch
and Budgets resources.

**On an AWS Free Plan account**, two defaults already account for the restrictions:
`c7i-flex.large` (a free-tier-eligible instance type) and `backup_retention_days = 1`.
On a standard account, raise the retention and enable the proxy:

```hcl
backup_retention_days = 7
enable_rds_proxy      = true
```

---

## 1. Infrastructure

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
#   admin_cidr   your IP:  curl -s https://checkip.amazonaws.com
#   alert_email  where budget and alarm notifications go

terraform init
terraform apply
```

About 20 minutes. Most of it is RDS and the DMS replication instance.

**Note the teardown command before going further.** See [Teardown](#teardown).

Outputs you will need:

```bash
terraform output sqlserver_instance_id
terraform output postgres_endpoint
terraform output analytics_bucket
```

---

## 2. Get onto the SQL Server host

There is no SSH key and no open port 22.

```bash
aws ssm start-session --target $(terraform output -raw sqlserver_instance_id)
```

You land as `ssm-user`, which cannot read the lab directory and does not have the Python
packages. Switch to root:

```bash
sudo -i
```

> Every step below also works unattended through `aws ssm send-command`, which is how they
> were originally run. If you do that, **`export HOME=/root` first** — see
> [Troubleshooting](#troubleshooting).

---

## 3. Clone and install the drivers

```bash
dnf install -y git
cd /home/ec2-user
git clone https://github.com/lubobali/mssql-to-postgres-migration-kit.git lab
cd lab/migration && bash bootstrap.sh
```

Installs the Microsoft ODBC driver 18, `pyodbc`, `psycopg`, `boto3`, `pytest` and `pyarrow`.
It ends by printing the versions and the detected ODBC driver — if that list is empty, stop
there, nothing downstream will work.

---

## 4. Build the legacy system

```bash
cd /home/ec2-user/lab/sqlserver
export AWS_REGION=us-east-2
bash setup.sh
```

This does five things:

1. Starts SQL Server 2022 in Docker, **with the Agent enabled** (CDC needs it)
2. `01_schema.sql` creates the schema, with all 12 migration traps
3. `generate_data.py` and `02_load.sql` produce and bulk load 250,000 transactions
4. `create_dms_user.py` creates the non-sysadmin login and **verifies it can connect**
5. `03_enable_cdc.sql` enables CDC on the database and all four tables

Steps 4 and 5 are what make DMS work at all, and they are related: CDC on SQL Server runs as
**Agent jobs**, and DMS only chooses the CDC path when the login is not `sysadmin`.

Expected:

```
merchants             50
transactions      250000
batches            12118
settlements        12118

case-insensitive (SQL Server default collation)   125504
case-sensitive (how PostgreSQL will behave)        41612

  sysadmin: no  ->  DMS will use MS-CDC
```

Idempotent. It removes the container **and its named volume**, because a volume outlives
`docker rm` and a rebuild that inherits the old schema is not a rebuild.

---

## 5. Apply the PostgreSQL schema

```bash
cd /home/ec2-user/lab/migration
python3 - <<'EOF'
import db
c = db.postgres_connection(); c.autocommit = True
c.execute(open("../postgres/01_schema.sql").read())          # converted schema
c.execute(open("../postgres/02_scheduled_jobs.sql").read())  # pg_cron job
c.execute(open("../postgres/03_microservice_split.sql").read())
c.close()
EOF
```

---

## 6. Migrate

Two paths. Run either, or both.

### The manual path

```bash
python3 migrate.py
```

About 15 seconds. Truncates the target first, because a reload always starts clean.

### AWS DMS with CDC

```bash
python3 run_dms.py
```

Tests both endpoints, starts the task, waits for full load, then **writes a row to SQL
Server and watches it arrive in PostgreSQL** — which is the whole point of CDC and the
reason a real cutover uses DMS.

```
full load complete in 60s
inserted txn_id 250001 on SQL Server
  arrived after 3.0s
```

**Stop the task when finished, or it keeps replicating and keeps billing:**

```bash
aws dms stop-replication-task --replication-task-arn $(cd terraform && terraform output -raw dms_task_arn)
```

---

## 7. Verify

```bash
cd /home/ec2-user/lab
python3 -m pytest -v
```

**All 15 must pass.** If any fail, see
[Remediation in FINDINGS.md](FINDINGS.md#remediation) — the right response depends on which
class of failure it is, and patching rows on the target is never it.

For the one-screen summary:

```bash
cd operations && PYTHONWARNINGS=ignore python3 show.py
```

---

## 8. Operations

### The nightly job

```sql
SELECT jobid, schedule, jobname, active FROM cron.job;
SELECT run_daily_settlement_summary(1);              -- run it by hand
SELECT job_name, status, rows_written, finished_at - started_at
FROM job_run_log ORDER BY run_id DESC LIMIT 10;      -- did it run?
```

`pg_cron` needs `shared_preload_libraries` in the parameter group **and a reboot**, plus
`cron.database_name`. Both are in `terraform/rds.tf`. It schedules in **UTC** regardless of
the server timezone.

### Backup and restore drill

```bash
cd /home/ec2-user/lab/operations
python3 backup_restore_drill.py
```

Snapshots, restores to a **new** instance, runs the verification profile against the restored
copy, then deletes it. About 12 minutes. Writes `restore_drill_result.json`.

### Performance

```bash
python3 performance.py
```

Measures, adds a covering index, measures again. Reports the result honestly including when
the index does not help — which, on this dataset, it does not.

### Analytics export

```bash
python3 export_to_s3.py --bucket $(cd ../../terraform && terraform output -raw analytics_bucket) --days 5
```

Writes Hive-partitioned Parquet to S3 and prints the Snowflake external stage DDL.

### Microservice reconciliation

```sql
SELECT * FROM settlement_svc.reconcile_orphans();
```

Must return zero for every row. Replaces the foreign keys once the schemas become separate
databases.

---

## Teardown

```bash
cd terraform
terraform destroy -auto-approve
```

Then confirm nothing survived:

```bash
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier'
aws dms describe-replication-instances --query 'ReplicationInstances[].ReplicationInstanceIdentifier'
aws ec2 describe-instances --filters "Name=tag:project,Values=rds-migration-lab" \
  --query 'Reservations[].Instances[].[InstanceId,State.Name]'
```

**Two things Terraform does not own:**

```bash
# Snapshots created by the restore drill
for s in $(aws rds describe-db-snapshots --snapshot-type manual \
    --query 'DBSnapshots[?starts_with(DBSnapshotIdentifier,`rds-migration-lab-drill`)].DBSnapshotIdentifier' \
    --output text); do
  aws rds delete-db-snapshot --db-snapshot-identifier "$s"
done

# DMS task logs
aws logs delete-log-group --log-group-name dms-tasks-rds-migration-lab-dms
```

Roughly **$2/day** while running. A budget alarm fires at 50% of $20, and it is created
before any billable resource exists.

---

## Troubleshooting

Every one of these happened.

| Symptom | Cause and fix |
|---|---|
| `fatal: $HOME not set` | SSM runs with no `HOME`, so `git config --global` fails silently, `safe.directory` never gets set, and every later git command dies. `export HOME=/root` |
| `dubious ownership in repository` | Same cause. Use `git -c safe.directory=/home/ec2-user/lab pull` |
| `Permission denied` on the lab directory | You are `ssm-user`. `sudo -i` |
| `No module named 'boto3'` | Packages were installed as root under `/root/.local`. Run as root, not `ec2-user` |
| Old schema keeps coming back | The Docker volume survived `docker rm`. `docker volume rm mssql-data` |
| `Cannot create more than one clustered index` | A PK is clustered by default. Declare it `NONCLUSTERED` |
| `SET options have incorrect settings: QUOTED_IDENTIFIER` | Needed by any statement touching a computed column, including `DELETE` |
| `Explicit value must be specified for identity column` | `BULK INSERT` needs `KEEPIDENTITY`. `SET IDENTITY_INSERT` does not apply to it |
| `IDENTITY_INSERT is already ON for table X` | An earlier `BULK INSERT` failed before its `OFF` ran. Fix the first error, not this one |
| Unicode arrives as `σîùµû╣` | `BULK INSERT` on Linux cannot read UTF-8. Convert with `iconv -t UTF-16` (**not** `UTF-16LE`, only the former writes a BOM) and use `DATAFILETYPE = 'widechar'` |
| `data file does not have a Unicode signature` | Missing BOM. Same fix. DMS then falls back to `char` and mangles the data **without failing** |
| `extension "pg_cron" is not available` | Not in stock PostgreSQL images. On RDS it needs the parameter group plus a reboot |
| `not eligible for Free Tier` | Account-level restriction, not a quota. Use a free-tier-eligible instance type |
| `Invalid ReplicationInstance class` | `dms.t3.micro` was retired. `aws dms describe-orderable-replication-instances` is the authority |
| `Password contains at least one unsupported characters : ;+%` | DMS rejects passwords RDS accepts. The charset must be the intersection of both |
| `The Distributor has not been installed correctly` | DMS chose MS-REPLICATION because the login is `sysadmin`. Connect as a non-sysadmin user and it selects MS-CDC |
| `Only members of the sysadmin fixed server role...` | Either `setUpMsCdcForTables=true` is set (remove it, it means "set CDC up FOR me") or `IgnoreMsReplicationEnablement=true` is missing |
| `SELECT permission was denied on the object 'fn_dblog'` | DMS's row validator reads the log directly. Grant it, and note a LOGIN is not a USER — you need one in `master` first |
| `Test connection ... should be successful for starting the replication task` | The endpoint changed. Re-run the connection test before starting |
