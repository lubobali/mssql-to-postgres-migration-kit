# ADR-001: RDS PostgreSQL vs Aurora PostgreSQL

**Status:** accepted for this workload
**Date:** 2026-08-24

---

## Context

The payments platform is moving off SQL Server onto managed PostgreSQL on AWS. There are two
managed PostgreSQL options and they are not interchangeable. Picking one because it is the
default is not a decision, it is an omission.

Requirements that actually constrain the choice:

- **Correctness before availability.** A settlement written twice is worse than a settlement
  written late.
- Recoverability has to meet a stated SLA, with a real recovery point objective.
- The application is moving toward microservices, so connection count grows faster than
  query volume.
- Multi-region is on the roadmap, described as "active-active".

---

## The comparison

| | RDS PostgreSQL | Aurora PostgreSQL |
|---|---|---|
| Storage | EBS volume attached to one instance | Distributed, 6 copies across 3 AZs |
| Failover | 60–120s (DNS + recovery) | Typically under 30s |
| Read replicas | Physical replication, each with its own storage | Share the same storage, millisecond lag |
| Replica lag | Seconds under load | Milliseconds |
| Scaling compute | Resize the instance, with downtime | Serverless v2 scales in place |
| Scaling storage | Autoscaling, one volume | Automatic to 128 TiB |
| Backups | Snapshots plus WAL | Continuous to S3 |
| Cross-region | Read replica, minutes of lag | Global Database, sub-second |
| Cost at small steady scale | Lower | Higher baseline |
| Cost at scale or spiky load | Higher | Better |
| Version availability | New PostgreSQL versions first | Lags upstream |

---

## Decision

**RDS PostgreSQL for the migration. Revisit Aurora when a specific requirement demands it.**

Reasoning:

**The migration should change one thing at a time.** Moving engines *and* moving to a
different storage architecture at once means any surprise has two possible causes. The
collation finding in [FINDINGS.md](FINDINGS.md) took real effort to characterise with only
one variable in play.

**Nothing in the current requirements needs Aurora.** Aurora earns its cost through read
replica fan-out, sub-30-second failover, and elastic scaling. This workload has one writer, a
predictable load profile, and no stated availability target that 60–120 seconds violates.

**Version currency favours RDS.** PostgreSQL 15.x was specified. RDS carries new minor
versions sooner, which matters for security patching in a PCI environment.

**Migrating RDS → Aurora later is a snapshot restore.** The decision is reversible and cheap
to revisit. That asymmetry is most of the argument.

### What would change the decision

- Read load needing more than two replicas, or replicas needing sub-second freshness
- A stated RTO under 60 seconds
- Load spiky enough that provisioned capacity is wasteful most of the day
- A genuine multi-region requirement (below)

---

## On "multi-region active-active"

This deserves its own answer, because the phrase is used loosely and getting it wrong in
payments is expensive.

**Aurora Global Database is not active-active.** It is one writer region and up to five
read-only secondaries with sub-second replication and fast promotion. That is
**active-passive with a good failover story** — genuinely valuable, and not what
"active-active" means. Calling it active-active in a design review is the kind of claim that
survives until an incident.

**True active-active with PostgreSQL compatibility** means Aurora DSQL, or accepting
application-level conflict resolution.

**And for payments, active-active writes are a correctness problem before they are an
infrastructure problem.** Two regions accepting writes for the same merchant means two
regions can settle the same batch. No amount of replication speed resolves that — it requires
either partitioning writes so a given merchant is only ever writable in one region, or
idempotency keys and conflict resolution designed into the application.

**Recommendation:** single-writer with a warm standby region, and merchant-partitioned writes
if genuine multi-region write capability is ever required. Two regions writing the same
settlement is worse than an hour of downtime.

---

## Cutover plan (either engine)

The engine choice does not change the shape of this.

1. **Schema first.** Convert and apply to the target. Verify with the type mapping table.
2. **DMS full load plus CDC.** Source stays live and keeps accepting writes.
3. **Wait for replication lag near zero.**
4. **Run the verification suite** against the moving target.
5. **Freeze writes briefly.** Minutes, not hours.
6. **Re-run verification** against a static target. This is the gate.
7. **Flip connection strings.**
8. **Keep the source readable and writable** until the new system survives a full settlement
   cycle. That is the rollback, and it is only real if nothing has been deleted.

For the later problem of *upgrading* the target with minimal downtime, **RDS Blue/Green
Deployments** builds a synchronised green environment and switches over in about a minute —
which is a different mechanism from this migration and worth not confusing with it.

---

## Connection pooling

Not optional once the application is microservices.

Each PostgreSQL connection is a backend process with its own memory. Ten services × ten pods
× a pool of twenty is 2,000 connections against an instance sized for a few hundred. The
database does not degrade gracefully — it stops accepting connections.

**RDS Proxy** sits in front, multiplexes many client connections onto few database ones, and
shortens failover by holding client connections open while the backend moves. It is the AWS
answer and it applies identically to RDS and Aurora.
