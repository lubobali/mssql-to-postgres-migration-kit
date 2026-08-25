# SQL Server → PostgreSQL Migration Kit

[![CI](https://github.com/lubobali/mssql-to-postgres-migration-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/lubobali/mssql-to-postgres-migration-kit/actions/workflows/ci.yml)

Moving a payments database off Microsoft SQL Server onto AWS-managed PostgreSQL, and
**proving the money still adds up on the other side.**

The problem this solves is the one that actually kills migrations. Not moving the data —
that part is easy. Knowing whether it arrived *correct*. A `MONEY` column silently losing
cents, or a timezone shifting settlement dates by a day, does not throw an error. It just
gives you wrong numbers, forever.

---

## The result

**The migration was perfect. The reports were wrong anyway.**

```
Legacy query returned      125,504 rows
Migrated query returns      41,612 rows
Silently no longer matched  83,892 rows  (66.8%)
```

Every row arrived. Every value is byte-identical. All 15 verification checks pass. And
`WHERE txn_status = 'Captured'` returns a third of what it used to, because SQL Server's
default collation is case-insensitive and PostgreSQL's is not.

No error. No warning. Nothing to grep for. A settlement report just gets quieter, and it
stays quiet until somebody reconciles against the bank.

That is not a bug a reload can fix. The data is provably intact — it is a change in what the
query *means*, and it needs a decision. See **[docs/FINDINGS.md](docs/FINDINGS.md)** for that
decision, and for the thirteen other things that broke on the way.

---

## Measured, not estimated

| | |
|---|---|
| Migration, manual path | **14.6s** for 250,000 transactions (17,151 rows/s) |
| Migration, AWS DMS full load | **60s** |
| **CDC replication lag** | **3.0s** — the source never stopped accepting writes |
| Snapshot | **196s** |
| **Restore to a new instance** | **515s** — this is the number an RTO is made of |
| Restore verified against live | row counts and money sums match |
| Verification suite | **15 passed**, locally and in CI |

---

## Architecture

```
     SOURCE                        MIGRATION                      TARGET
┌──────────────────┐        ┌────────────────────┐        ┌──────────────────────┐
│ SQL Server 2022  │───────▶│  AWS DMS           │───────▶│ RDS PostgreSQL 15    │
│ EC2, MS-CDC on   │  CDC   │  full load + CDC   │        │ private, encrypted   │
│ no SSH, no :22   │        │  dms.t3.small      │        │ pg_cron, PI, IAM auth│
└──────────────────┘        └────────────────────┘        └──────────┬───────────┘
        │                                                            │
        │                   ┌────────────────────┐                   │
        └──────────────────▶│  manual path       │──────────────────▶│
                            │  typed, idempotent │                   │
                            └────────────────────┘                   │
                                                                     │
        ┌────────────────────────────────────────────────────────────┤
        │                            │                               │
┌───────▼──────────┐  ┌──────────────▼───────┐  ┌───────────────────▼──────────┐
│ VERIFICATION     │  │ OPERATIONS           │  │ ANALYTICS                    │
│ 15 pytest checks │  │ snapshot → restore   │  │ Parquet → S3, partitioned    │
│ both engines     │  │ verified, timed      │  │ decimal128(19,4), not float  │
│ FAILS on drift   │  │ pg_cron nightly job  │  │ Snowflake external stage     │
│ runs in CI       │  │ 4 CloudWatch alarms  │  │                              │
└──────────────────┘  └──────────────────────┘  └──────────────────────────────┘

   Every AWS resource is Terraform. Nothing is created by hand.
```

---

## Why the boring parts are the point

**Verification is a gate, not a report.** Row counts, every money column summed to four
decimals as `Decimal`, min/max on every date column, per-column NULL counts, per-merchant
checksums, identity sequence high-water marks, unicode matched exactly. It exits non-zero
when anything differs, and it runs in CI.

**And it is anchored outside both databases.** A suite that only diffs source against target
can prove the *move* was faithful — never that the thing being moved was right. That gap is
not theoretical: `BULK INSERT` on Linux silently loaded `σîùµû╣τë⌐µ╡ü` where `北方物流`
belonged, the migration carried the corruption across perfectly, and **13 of 14 checks
passed.**

**Failures are classified, because they need different responses.** Rows missing means fix
and reload. Values differing means fix the mapping and reload. Query results differing is not
a data bug at all, and reloading will never help it. Getting that wrong is worse than the
original problem — and patching rows on the target is never the answer, because a
hand-patched database matches nothing and cannot be proven.

**Negative results are reported as results.** A covering index bought 0.2ms and cost 12 MB of
write amplification. Quoting best-of-N instead of the median would have made that a win in a
slide, and left the cost in production.

---

## Security posture

| | |
|---|---|
| SSH | **No key pair, no port 22.** Access is SSM Session Manager |
| Database | Not publicly accessible. Reachable only from inside the VPC |
| Security groups | Postgres accepts **security group identities**, not IP ranges |
| Secrets | Generated by Terraform into Secrets Manager, never in a file |
| IAM | Scoped to named ARNs and name patterns, never `service:*` on `*` |
| DMS login | Non-sysadmin and dedicated — also what makes MS-CDC work at all |
| Storage | Encrypted at rest — EBS, RDS and S3 |
| Instance metadata | IMDSv2 required |
| Data | **No real data anywhere.** Generated merchants, fake card last-4 |

---

## Stack

Terraform · AWS RDS PostgreSQL 15 · SQL Server 2022 · **AWS DMS with CDC** ·
Secrets Manager · SSM Session Manager · Performance Insights · pg_stat_statements ·
pg_cron · CloudWatch · SNS · S3 · Parquet · Python · pytest · GitHub Actions

**Configured but not enabled:** RDS Proxy — unavailable on AWS Free Plan accounts. The
Terraform is complete and sits behind a flag.

---

## Layout

```
terraform/     VPC, RDS, EC2, DMS, security groups, secrets, alarms, S3, budget
sqlserver/     legacy schema, data generator, bulk load, CDC, the DMS login
postgres/      converted schema, the rebuilt scheduled job, the microservice split
migration/     profiling, the manual migration, the DMS task runner
verification/  the 15 checks that decide whether the migration passed
operations/    timed restore drill, performance measurement, the S3 export
ci/            the pipeline, adapted to run on a bare runner
docs/          FINDINGS, RUNBOOK, and the RDS-vs-Aurora decision record
```

---

## Read these

- **[docs/FINDINGS.md](docs/FINDINGS.md)** — fourteen failures, every one from running it
  rather than reading about it, each written up with what it costs
- **[docs/ADR-001](docs/ADR-001-rds-vs-aurora.md)** — RDS vs Aurora, and why Aurora Global
  Database is **not** active-active
- **[docs/RUNBOOK.md](docs/RUNBOOK.md)** — build it from nothing, and take it apart
- **[PLAN.md](PLAN.md)** — the original plan, including the parts that turned out wrong

---

## Running it

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # your IP, your email
terraform init && terraform apply
```

Then follow **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

Tear down completely:

```bash
terraform destroy
```

Everything is tagged `project=rds-migration-lab`. Roughly **$2/day** while running, with a
budget alarm created before any billable resource exists.

**No real data is used anywhere in this project.**
