# MS SQL Server → AWS PostgreSQL — Migration Toolkit

A working rehearsal of a legacy payments database moving from Microsoft SQL Server to
AWS-managed PostgreSQL, with the verification, cutover, and operational layers a real
migration needs.

**Built the way the owner of the database would build it, not the way a candidate would.**

---

## The premise

If you own the database at a payments company, nobody hands you a plan. You are the one who
writes it, defends it, and gets paged when it goes wrong. So this project is scoped as if the
decision were yours:

- Infrastructure is **code**, not clicks and not shell scripts
- The migration path is the one a real company would use, **and** the manual one, so the
  tradeoff is understood rather than recited
- Verification is **tested**, in CI, not eyeballed
- The cutover plan matters as much as the data move
- Architecture choices are **evaluated and recommended**, not just used

---

## What it does, in two sentences

It moves a payments database off Microsoft SQL Server onto AWS PostgreSQL, and proves the
money still adds up on the other side.

The problem it solves is that migrated data can arrive **wrong without anything looking
broken** — no error, no crash, just cents quietly disappearing and dates shifting by a day.

---

## Grounding: what the job description actually asks for

Direct quotes, because the project should answer these and not something adjacent.

> "modernizing the database infrastructure by **migrating from MS SQL server to the AWS RDS
> environment**"

> "Ensure database availability and **recoverability** meet established Service Level
> Agreements (SLAs)"

> "**Automate** system administration with a focus on resource efficiency and scalability"

> "Design, implement and maintain **database security** based on best practices, company
> security standards and **regulatory compliance**"

> "Migrate legacy database structures to support the modernization of the payment processing
> platform using **microservice architecture**"

> "Support the **Disaster Recovery (DR)** strategy and a **multi-region active-active setup**"

> "Support **data pipeline automation to Snowflake**"

> "Experience with Database **High Availability / Replication** technologies (e.g. AlwaysOn,
> Postgres HA)"

> "Proven experience with AWS technologies: **Postgres RDS, SQL Server RDS, Aurora RDS and EC2**"

> "**Scripting and automation** experience"

Note what is in there that a small project usually skips: multi-region active-active, DR,
Aurora specifically, and Snowflake. Those get real answers below, even where they are
documented rather than built.

---

## What changed from the first draft, and why

The first version of this plan was a competent mid-level project. Three things were wrong:

**1. It created infrastructure with CLI commands.** Nobody senior owns production that way.
Infrastructure is Terraform, in git, reviewable, destroyable. He already uses Terraform on
`aws-job-streamer`, so doing it by hand here would be a step backwards.

**2. It hand-rolled the migration and mentioned DMS as an afterthought.** Backwards. The
person who owns this needs to have actually driven **AWS DMS**, because that is what a real
cutover uses — continuous replication with CDC so the source stays live until the moment you
switch. Hand-rolling is still worth doing, but as the thing that teaches you what DMS is
doing underneath, not as the plan.

**3. It ignored the architecture decision entirely.** The JD names **Aurora**, **multi-region
active-active**, and **HA/replication**. An owner does not just pick RDS because it is the
default — they evaluate RDS vs Aurora and can defend the answer. That evaluation is a
deliverable here.

---

## Stack

Chosen to match what a payments company running on AWS in 2026 would actually use.

| Layer | Choice | Why this and not the obvious alternative |
|---|---|---|
| Source database | SQL Server 2022 Developer, Docker | Free, and matches the JD's "MS SQL 2019/2022" |
| Target database | **RDS PostgreSQL 15** | JD names 15.x specifically |
| Also evaluated | **Aurora PostgreSQL Serverless v2** | JD names Aurora; the RDS-vs-Aurora writeup is a deliverable |
| Infrastructure | **Terraform** | Not CLI, not console. Version controlled and destroyable |
| Migration engine | **AWS DMS** with CDC | The real tool. Full load + ongoing replication |
| Schema conversion | **DMS Schema Conversion**, plus by hand | Tool first for speed, by hand to actually learn the mappings |
| Schema versioning | **Alembic** | Migrations as versioned code, not loose `.sql` files |
| Secrets | **AWS Secrets Manager** + **IAM database auth** | No passwords in scripts. IAM auth means no password at all |
| Connection pooling | **RDS Proxy** | Microservices exhaust Postgres connections; this is the AWS answer |
| Observability | **Performance Insights** + `pg_stat_statements` + CloudWatch | PI is how you actually answer "why is it slow" |
| Scheduled jobs | **pg_cron** | Because RDS Postgres has no SQL Server Agent |
| Verification | **pytest** | His signature. Verification that is not tested is not verification |
| CI | **GitHub Actions** | The suite runs on every push |
| Analytics export | **S3 + Parquet** | Snowflake reads Parquet natively via external stage |

**Deliberately not used:** the AWS console for anything that matters, `float` for money,
and any step that cannot be re-run.

---

## Architecture

```
     SOURCE                          MIGRATION                      TARGET
┌──────────────────┐          ┌────────────────────┐        ┌──────────────────────┐
│ SQL Server 2022  │          │  AWS DMS           │        │ RDS PostgreSQL 15    │
│ Docker, Hetzner  │─────────▶│  full load + CDC   │───────▶│ Multi-AZ capable     │
│ "legacy prod"    │          │  replication task  │        │ encrypted at rest    │
└──────────────────┘          └────────────────────┘        └──────────┬───────────┘
        │                                                              │
        │                     ┌────────────────────┐                   │
        │                     │  Manual path       │                   │
        └────────────────────▶│  extract → COPY    │──────────────────▶│
                              │  (the learning)    │                   │
                              └────────────────────┘                   │
                                                                       │
                    ┌──────────────────────────────────────────────────┤
                    │                                                  │
         ┌──────────▼──────────┐                          ┌────────────▼────────────┐
         │ VERIFICATION        │                          │ OPERATIONS              │
         │ pytest, both sides  │                          │ snapshot → restore      │
         │ counts, sums, dates │                          │ pg_cron scheduled job   │
         │ checksums, nulls    │                          │ Performance Insights    │
         │ FAILS on mismatch   │                          │ RDS Proxy               │
         └─────────────────────┘                          │ S3 Parquet → Snowflake  │
                                                          └─────────────────────────┘

  All AWS resources defined in Terraform. Nothing created by hand.
```

---

## The schema — payments shaped, deliberately hostile

Small enough to finish. Every type chosen because it breaks something on the way across.

```sql
merchants     merchant_id    INT IDENTITY(1,1)
              merchant_guid  UNIQUEIDENTIFIER      -- byte-order trap
              name           NVARCHAR(200)
              mcc            CHAR(4)               -- fixed-width padding trap
              is_active      BIT                   -- 0/1 vs true/false
              created_at     DATETIME2             -- timezone trap

transactions  txn_id         BIGINT IDENTITY(1,1)
              merchant_id    INT
              amount         MONEY                 -- the money trap
              currency       CHAR(3)
              card_last4     CHAR(4)
              auth_code      VARCHAR(6)
              status         NVARCHAR(20)          -- case-sensitivity trap
              captured_at    DATETIME2

batches       batch_id, merchant_id, batch_date DATE, txn_count, gross_amount MONEY
settlements   settlement_id, batch_id, net_amount MONEY, fee_amount MONEY, settled_at DATETIME2
```

Plus **one stored procedure** (`sp_daily_settlement_summary`) because stored procedures do not
port, and **one SQL Server Agent job** because RDS Postgres has no Agent.

Seed: ~50 merchants, ~250,000 transactions. Large enough that a bad load is visibly slow and
`EXPLAIN` results are meaningful. No real data — generated merchants, fake card last-4,
generated auth codes.

---

## Phases

Ordered so that stopping early still leaves something coherent.

### Phase 0 — Ground

- `aws configure`, confirm the account, confirm the region
- Write the **teardown** before creating anything
- `git init`, repo structure, `.gitignore` blocking `.tfstate`, `.env`, `*.pem`, any CSV
- Terraform backend and provider pinned to explicit versions
- **Cost guardrail:** an AWS Budget alarm at $10 so a forgotten resource cannot become a
  surprise

**Done means:** `terraform plan` runs against an empty config, and the teardown command exists
in writing before the first resource does.

---

### Phase 1 — The legacy system

- SQL Server 2022 in Docker on Hetzner (x86; Apple Silicon cannot run it natively)
- Schema in genuinely SQL-Server-flavoured DDL
- The stored procedure and the Agent job
- Seed 250k transactions
- **Capture the baseline**: row counts, `SUM` of every money column, `MIN`/`MAX` of every date,
  NULL counts per column, per-merchant checksums — written to `baseline.json`

**Done means:** `baseline.json` exists. That file is the definition of truth for the rest of
the project.

---

### Phase 2 — Infrastructure as code

Everything in Terraform. Nothing clicked.

- VPC, subnet group, **security group allowing only the Hetzner IP**
- RDS PostgreSQL 15, `db.t4g.micro`, **encrypted at rest**, not publicly accessible
- Parameter group: `shared_preload_libraries = pg_cron,pg_stat_statements`
- **Secrets Manager** for the master credential — never in a file, never in git
- **IAM database authentication enabled** — the app role connects with a token, not a password
- **Performance Insights** on (7-day retention is free)
- Roles created as code: `app_rw`, `app_ro`, `migration_svc`. **The application never uses the
  master user.**
- Everything tagged `project=rds-migration-lab`

**Done means:** `terraform apply` produces a database, `terraform destroy` removes every trace,
and `SELECT current_user` is not the master account.

---

### Phase 3 — Schema conversion  ← *where the learning is*

Run **DMS Schema Conversion** first. Then convert by hand and diff the two.

The diff is the deliverable: where the tool was right, where it was lazy, and where it would
have silently hurt you.

| SQL Server | PostgreSQL | The actual risk |
|---|---|---|
| `IDENTITY(1,1)` | `GENERATED BY DEFAULT AS IDENTITY` | Sequence not reset after load → PK collisions on first insert |
| `NVARCHAR(n)` | `varchar(n)` / `text` | Postgres is UTF-8 throughout; `text` has no penalty |
| `DATETIME2` | `timestamptz` | **Timezone.** Pick wrong and settlement dates move a day |
| `BIT` | `boolean` | `0/1` → `true/false` breaks every downstream query |
| `MONEY` | `numeric(19,4)` | **Never `float`.** Rounding loses cents. Unacceptable in payments |
| `UNIQUEIDENTIFIER` | `uuid` | Byte order differs; string form is safe, binary is not |
| `CHAR(n)` | `char(n)` | Trailing-space semantics differ between engines |
| Default collation | (case-sensitive) | **The dangerous one.** Query results change with no error |
| Clustered index | — | Postgres has none; `CLUSTER` is one-time, not maintained |
| Stored procedure | PL/pgSQL function | Rewrite, not convert |
| `GETDATE()` / `ISNULL()` / `TOP` | `now()` / `COALESCE()` / `LIMIT` | Mechanical |

**Schema lands via Alembic**, not a raw `.sql` file, so it is versioned and repeatable across
dev, test, and prod — which is literally a JD line item.

**Done means:** the table above is filled in with what actually happened, and the tool-vs-hand
diff is written down.

---

### Phase 4 — Data movement, both ways

**A. AWS DMS** — replication instance, source and target endpoints, a full-load-plus-CDC task.
Let it reach ongoing replication, then write to SQL Server and watch it appear in Postgres.
That is the cutover mechanism, and it is the thing worth having actually seen.

**B. By hand** — extract to Parquet, load with `COPY`, idempotent, re-runnable.

The point of doing both: DMS is what you would use, and the manual path is how you know what
DMS is doing. **Also record how each one handled the type traps** — they will not agree.

**Done means:** both paths land the data, and a row inserted into SQL Server appears in
Postgres through CDC without re-running anything.

---

### Phase 5 — Verification, as a test suite  ← *the part that matters most*

Not a script that prints. A **pytest suite** that fails.

```
test_row_counts_match
test_money_sums_match_to_the_cent
test_date_ranges_match          # catches timezone shift
test_null_counts_match          # catches silent conversion failure
test_per_merchant_checksums     # catches rows under the wrong parent
test_sampled_row_hashes         # catches truncation
test_sequence_high_water_mark   # catches the IDENTITY reset trap
test_no_orphaned_foreign_keys
```

**Then break it deliberately** — change one amount by a cent, shift one timezone, drop one row
— and confirm every test catches its own failure. A suite that has never gone red is not a
suite.

**Runs in CI on every push.**

**Done means:** CI is green, and there is a commit showing it red for the right reason.

---

### Phase 6 — The operational layer

This is where a database *owner* separates from a data engineer.

- **Recoverability:** take a snapshot, restore to a new instance via Terraform, run the
  verification suite against the restored copy, and **record how long it took.** That number
  is what an SLA is actually made of.
- **PITR:** document the retention window and what it does and does not guarantee.
- **The scheduled job problem:** RDS Postgres has no SQL Server Agent. Rebuild the nightly
  settlement summary with `pg_cron`. Document the alternatives — Lambda + EventBridge, Airflow
  — and when each is right.
- **Performance:** `pgbench` for a baseline, then find a genuinely slow query, read it in
  **Performance Insights**, `EXPLAIN (ANALYZE, BUFFERS)`, add the index, measure. Keep both
  plans as artifacts.
- **Monitoring:** CloudWatch alarms that mean something — free storage, connection count,
  replica lag — into an SNS topic. Not a dashboard nobody reads.
- **RDS Proxy** in front, because the JD's microservice architecture will exhaust Postgres
  connections without it, and it also shortens failover.

**Done means:** a restore actually happened with a timing number, and one query is measurably
faster with the plan to prove it.

---

### Phase 7 — Architecture decision record

The senior deliverable. A written recommendation, not a preference.

**`ADR-001: RDS PostgreSQL vs Aurora PostgreSQL`**

| | RDS PostgreSQL | Aurora PostgreSQL |
|---|---|---|
| Storage | EBS attached | Distributed, 6 copies across 3 AZs |
| Failover | 60-120s | Typically under 30s |
| Read replicas | Physical replication, own storage | Share storage, ~ms lag |
| Scaling | Resize instance | Serverless v2 scales in-place |
| Backups | Snapshots | Continuous to S3 |
| Cost | Lower at steady small scale | Higher baseline, better at scale |
| Multi-region | Cross-region read replica | **Global Database**, sub-second lag |

Then the JD's hardest line — **"multi-region active-active"**:

- **Aurora Global Database** is fast cross-region but **single writer**. It is active-passive
  with quick promotion, not active-active. Calling it active-active is the mistake to avoid.
- True active-active with Postgres compatibility means **Aurora DSQL**, or accepting
  application-level conflict resolution.
- For payments specifically, **active-active writes are a correctness problem before they are
  an infrastructure problem** — two regions writing the same settlement is worse than downtime.

Having a real opinion on that, with the tradeoff named, is worth more than any amount of
hands-on-keyboard.

**Also documented:** the **cutover plan** — DMS CDC to steady state, freeze writes, verify,
flip connection strings, keep the source readable for rollback. And **RDS Blue/Green
Deployments** as the modern low-downtime mechanism for the version-upgrade case.

---

### Phase 8 — The two JD lines nobody builds

**Microservice split.** Take the wide legacy tables into service-owned schemas —
`merchant_svc`, `txn_svc`, `settlement_svc` — with explicit boundaries. Document what breaks:
cross-schema foreign keys stop being enforceable, and joins that used to be free become
network calls. That tension *is* the microservice tradeoff.

**Snowflake path.** Export transactions from Postgres to **S3 as Parquet**, partitioned by
date. Snowflake reads it natively through an external stage. This is already his LuBot skill,
and it answers the JD line without needing a Snowflake account.

---

### Phase 9 — Write it up

`FINDINGS.md` — the real artifact:

1. What moved, how much, how long
2. **The type mapping table with what actually broke**
3. DMS vs hand conversion: where they disagreed
4. Verification approach, and the commit where it caught a real error
5. Restore timing
6. What has no equivalent in RDS Postgres
7. The architecture recommendation and the cutover plan
8. What would be different at 100× the data

`RUNBOOK.md` — run the whole thing from zero.
`ADR-001.md` — the RDS vs Aurora decision.

---

## Cost

| Resource | Rate | Two days |
|---|---|---|
| RDS `db.t4g.micro` | ~$0.016/hr | ~$0.80 |
| DMS `dms.t3.micro` | ~$0.018/hr | ~$0.90 |
| Aurora Serverless v2 (if evaluated) | ~$0.06/ACU-hr | ~$1.50 |
| RDS Proxy | ~$0.015/hr | ~$0.70 |
| S3, Secrets Manager, CloudWatch | pennies | — |

**Under $5 total.** An AWS Budget alarm at $10 goes in during Phase 0.

Teardown, written before anything is created:

```bash
cd terraform && terraform destroy -auto-approve

# then confirm nothing survived
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier'
aws dms describe-replication-instances --query 'ReplicationInstances[].ReplicationInstanceIdentifier'
aws rds describe-db-proxies --query 'DBProxies[].DBProxyName'
```

---

## Isolation

- **`aws-job-streamer` and the LuBot Parquet pipeline are untouchable.** Separate VPC,
  separate security group, separate everything.
- Everything tagged `project=rds-migration-lab`
- RDS not publicly accessible; only the Hetzner IP allowed
- **No real data anywhere.** Generated merchants, fake card last-4, generated auth codes.
- The repo is generic — a SQL Server to Postgres migration toolkit. **No employer name in it.**
  It is reusable, and it can be public.

---

## Priority if time runs out

Wednesday is the deadline for *having something to say*, not for finishing.

1. **Phase 1 + Phase 3 + Phase 5** — baseline, schema conversion, verification. This is the
   entire story and it stands alone.
2. **Phase 6 backup/restore** — one real timing number beats any amount of theory
3. **Phase 6 scheduled jobs** — the single smartest observation in the project
4. **Phase 7 ADR** — costs an hour of thinking, no infrastructure, highest senior signal
5. Phase 4 DMS — high value, most setup friction
6. Phase 8 stretch goals

Being mid-build on Wednesday is fine and arguably better. "Here is what I am building and what
has bitten me so far" is a live conversation. A finished project is a monologue.
