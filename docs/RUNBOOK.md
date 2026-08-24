# Runbook

How to build this from nothing, and how to take it apart.

---

## Prerequisites

```bash
brew install awscli terraform
aws configure          # region us-east-2, output json
```

The AWS identity needs permission to create VPC, EC2, RDS, IAM, Secrets Manager and Budgets
resources.

**If the account is on the AWS Free Plan**, only free-tier-eligible instance types are
permitted and backup retention is capped. Both defaults here already account for that —
`c7i-flex.large` and `backup_retention_days = 1`. On a standard account, raise the retention:

```hcl
backup_retention_days = 7
```

---

## Build

### 1. Infrastructure

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# set admin_cidr to your own IP:  curl -s https://checkip.amazonaws.com
# set alert_email for the budget alarm

terraform init
terraform apply
```

Roughly 10 minutes, most of it waiting for RDS. Outputs print the instance id and the
Postgres endpoint.

**Before anything else**, note the teardown command. See [Teardown](#teardown).

### 2. Get onto the SQL Server host

There is no SSH key and no open port 22.

```bash
aws ssm start-session --target $(terraform output -raw sqlserver_instance_id)
```

Requires the Session Manager plugin locally. Without it, use `aws ssm send-command` — every
step below works that way, which is also how they run unattended.

### 3. Clone and install the drivers

```bash
sudo dnf install -y git
cd /home/ec2-user
git clone https://github.com/lubobali/mssql-to-postgres-migration-kit.git lab
cd lab/migration && bash bootstrap.sh
```

> **If you drive this through `aws ssm send-command`, export `HOME` first.** SSM runs with no
> `HOME`, so `git config --global` fails, `safe.directory` never gets set, and every
> subsequent git command dies with "dubious ownership" — silently, while the pull appears to
> succeed and you debug stale code for an hour.
>
> ```bash
> export HOME=/root
> git -c safe.directory=/home/ec2-user/lab pull
> ```

### 4. Build the legacy system

```bash
cd /home/ec2-user/lab/sqlserver
export AWS_REGION=us-east-2
bash setup.sh
```

Starts SQL Server 2022 in Docker, creates the schema, generates 250,000 transactions, and
bulk loads them. Idempotent — it removes the container **and its volume**, because a named
volume outlives `docker rm` and a rebuild that inherits the previous schema is not a rebuild.

Expected:

```
merchants             50
transactions      250000
batches            12118
settlements        12118

case-insensitive (SQL Server default collation)   125504
case-sensitive (how PostgreSQL will behave)        41612
```

### 5. Apply the PostgreSQL schema

```bash
cd /home/ec2-user/lab/migration
python3 - <<'EOF'
import db
c = db.postgres_connection(); c.autocommit = True
c.execute(open("../postgres/01_schema.sql").read())
c.execute(open("../postgres/02_scheduled_jobs.sql").read())
c.close()
EOF
```

### 6. Migrate

```bash
python3 migrate.py
```

About 15 seconds. Truncates the target first — a reload always starts clean.

### 7. Verify

```bash
cd /home/ec2-user/lab
python3 -m pytest -v
```

**15 tests must pass.** If any fail, see
[Remediation in FINDINGS.md](FINDINGS.md#remediation) — the response depends on which class
of failure it is, and patching rows on the target is never the answer.

---

## Operations

### The nightly job

RDS PostgreSQL has no SQL Server Agent, so the job is `pg_cron`:

```sql
SELECT jobid, schedule, jobname, active FROM cron.job;

-- run it by hand
SELECT run_daily_settlement_summary(1);

-- did it run?
SELECT job_name, status, rows_written, finished_at - started_at AS duration
FROM job_run_log ORDER BY run_id DESC LIMIT 10;
```

`pg_cron` requires `shared_preload_libraries = pg_cron` in the parameter group **and a
reboot**, plus `cron.database_name` naming the database. Both are set in
`terraform/rds.tf`. It schedules in **UTC** regardless of the server timezone.

### Backup and restore drill

```bash
cd /home/ec2-user/lab/operations
python3 backup_restore_drill.py
```

Snapshots, restores to a **new** instance, runs the verification profile against the restored
copy, then deletes it. Writes `restore_drill_result.json`.

The verification step is the point. A snapshot that restores into a database nobody queried
is a file, not a recovery plan.

### Performance

```sql
-- slowest statements
SELECT calls, round(mean_exec_time::numeric, 2) AS avg_ms, query
FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;

-- and a plan
EXPLAIN (ANALYZE, BUFFERS)
SELECT ... ;
```

Performance Insights is enabled with 7-day retention (free tier), and is the better tool for
"why was it slow yesterday".

---

## Teardown

```bash
cd terraform
terraform destroy -auto-approve
```

Then confirm nothing survived:

```bash
aws rds describe-db-instances  --query 'DBInstances[].DBInstanceIdentifier'
aws rds describe-db-snapshots  --query 'DBSnapshots[?starts_with(DBSnapshotIdentifier, `rds-migration-lab`)].DBSnapshotIdentifier'
aws ec2 describe-instances     --filters "Name=tag:project,Values=rds-migration-lab" \
                               --query 'Reservations[].Instances[].[InstanceId,State.Name]'
```

**Snapshots created by the drill are not managed by Terraform** and will not be destroyed
with it. Delete them explicitly:

```bash
for s in $(aws rds describe-db-snapshots \
    --query 'DBSnapshots[?starts_with(DBSnapshotIdentifier, `rds-migration-lab-drill`)].DBSnapshotIdentifier' \
    --output text); do
  aws rds delete-db-snapshot --db-snapshot-identifier "$s"
done
```

Running cost is roughly **$1.50/day**. The budget alarm fires at 50% of $20.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `fatal: $HOME not set` | SSM runs without `HOME`. `export HOME=/root` |
| `dubious ownership in repository` | Same cause. Use `git -c safe.directory=<path>` |
| Old schema keeps coming back | The Docker volume survived `docker rm`. `docker volume rm mssql-data` |
| `Cannot create more than one clustered index` | The PK is clustered by default. Declare it `NONCLUSTERED` |
| `SET options have incorrect settings: QUOTED_IDENTIFIER` | `SET QUOTED_IDENTIFIER ON` — needed by any statement touching a computed column, including `DELETE` |
| `Explicit value must be specified for identity column` | `BULK INSERT` needs `KEEPIDENTITY`; `SET IDENTITY_INSERT` does not apply to it |
| `IDENTITY_INSERT is already ON for table X` | An earlier `BULK INSERT` failed before its `OFF` ran. Fix the first error, not this one |
| Unicode arrives as `σîùµû╣` | `BULK INSERT` on Linux cannot read UTF-8. Convert with `iconv -t UTF-16` (**not** `UTF-16LE` — only the former writes a BOM) and use `DATAFILETYPE = 'widechar'` |
| `data file does not have a Unicode signature` | Missing BOM. Same fix. It then falls back to `char` and mangles the data **without failing** |
| `extension "pg_cron" is not available` | Not in stock PostgreSQL images. On RDS it needs the parameter group plus a reboot |
| `not eligible for Free Tier` | Account-level restriction, not a quota. Use a free-tier-eligible instance type |
