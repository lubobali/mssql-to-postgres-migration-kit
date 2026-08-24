/* ═══════════════════════════════════════════════════════════════════
   Splitting the legacy tables into service-owned schemas.

   The job description asks to "migrate legacy database structures to
   support the modernization of the payment processing platform using
   microservice architecture."

   This is the honest version of that, including what it costs — because
   the interesting part is not the split, it is what stops working
   afterwards.

   ───────────────────────────────────────────────────────────────────
   What actually breaks

   1. FOREIGN KEYS STOP BEING ENFORCEABLE ACROSS SERVICES.

      A foreign key from txn_svc.transactions to merchant_svc.merchants
      works while both live in one database. The moment merchant_svc gets
      its own database — which is the point of the exercise — the
      constraint cannot exist. Referential integrity becomes the
      application's problem, enforced by convention and discovered by a
      reconciliation job.

   2. JOINS THAT WERE FREE BECOME NETWORK CALLS.

      The settlement report joins transactions to merchants. In one
      database that is a hash join. Across services it is an API call
      per merchant, or a cached copy of the merchant list that is
      sometimes stale.

   3. TRANSACTIONS STOP BEING ATOMIC.

      "Insert the transaction and update the batch total" is one commit
      today. Split across services it is two, and something has to
      handle the case where the second fails — an outbox, a saga, or
      accepting eventual consistency and reconciling.

      In payments that third option needs a real decision, not a default.

   ───────────────────────────────────────────────────────────────────
   So this migration is deliberately a HALFWAY step: separate schemas,
   same database. Ownership boundaries become explicit and enforceable
   through grants, cross-boundary access becomes visible, and nothing
   has been given up yet.

   That is the useful thing to do during an engine migration. Splitting
   into separate databases at the same time means any surprise has two
   possible causes.
   ══════════════════════════════════════════════════════════════════ */

CREATE SCHEMA IF NOT EXISTS merchant_svc;
CREATE SCHEMA IF NOT EXISTS txn_svc;
CREATE SCHEMA IF NOT EXISTS settlement_svc;

/* ─── Service roles ───────────────────────────────────────────────── */

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['merchant_svc_role', 'txn_svc_role', 'settlement_svc_role']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END
$$;

/* ─── Views marking the boundary ──────────────────────────────────── */

/* Views rather than moved tables, deliberately.

   Moving the tables means a cutover per service and a rollback plan per
   service. Views make the boundary real and enforceable through grants
   TODAY, with zero data movement, and the tables can move later one at
   a time behind an interface that already exists.

   This is the same reasoning as the engine migration itself: change one
   thing, verify it, then change the next. */

CREATE OR REPLACE VIEW merchant_svc.merchants AS
    SELECT merchant_id, merchant_guid, legal_name, dba_name, mcc,
           is_active, onboarding_status, created_at, updated_at
    FROM public.merchants;

CREATE OR REPLACE VIEW txn_svc.transactions AS
    SELECT txn_id, merchant_id, amount, fee_amount, currency,
           card_last4, auth_code, txn_status, captured_at, settled_at
    FROM public.transactions;

CREATE OR REPLACE VIEW settlement_svc.batches AS
    SELECT batch_id, merchant_id, batch_date, txn_count,
           gross_amount, batch_status
    FROM public.batches;

CREATE OR REPLACE VIEW settlement_svc.settlements AS
    SELECT settlement_id, batch_id, net_amount, fee_amount,
           gross_amount, settled_at
    FROM public.settlements;

/* ─── The published contract between services ─────────────────────── */

/* txn_svc needs to know a merchant exists and is active. It does NOT
   need the merchant's legal name, MCC, or onboarding status.

   This view IS the interface. Once it is the only thing granted across
   the boundary, adding a column to merchants cannot silently become
   another service's dependency — which is how a "microservice" ends up
   unable to deploy without three other teams. */

CREATE OR REPLACE VIEW merchant_svc.merchant_public AS
    SELECT merchant_id,
           merchant_guid,
           is_active
    FROM public.merchants;

COMMENT ON VIEW merchant_svc.merchant_public IS
    'Published contract. The only merchant data other services may read. '
    'Adding a column here is an API change and needs the same care as one.';

/* ─── Grants: each service owns its own and reads only the contract ── */

GRANT USAGE ON SCHEMA merchant_svc   TO merchant_svc_role, txn_svc_role, settlement_svc_role;
GRANT USAGE ON SCHEMA txn_svc        TO txn_svc_role, settlement_svc_role;
GRANT USAGE ON SCHEMA settlement_svc TO settlement_svc_role;

GRANT SELECT ON ALL TABLES IN SCHEMA merchant_svc   TO merchant_svc_role;
GRANT SELECT ON ALL TABLES IN SCHEMA txn_svc        TO txn_svc_role;
GRANT SELECT ON ALL TABLES IN SCHEMA settlement_svc TO settlement_svc_role;

-- Across the boundary: the contract only.
GRANT SELECT ON merchant_svc.merchant_public TO txn_svc_role, settlement_svc_role;

-- settlement_svc reads transactions because settlement is derived from
-- them. Named explicitly so the dependency is visible in the grants
-- rather than discovered in a query plan.
GRANT SELECT ON txn_svc.transactions TO settlement_svc_role;

/* ─── The reconciliation that replaces the foreign key ────────────── */

/* Once these schemas are separate databases, the FK from transactions to
   merchants is gone. Something has to notice an orphan, and "something"
   is a scheduled job, not a constraint.

   Written now, while the FK still exists and the answer is provably
   zero. A check first written after the split has no known-good
   baseline to compare against. */

CREATE OR REPLACE FUNCTION settlement_svc.reconcile_orphans()
RETURNS TABLE (check_name text, orphan_count bigint)
LANGUAGE sql
STABLE
AS $$
    SELECT 'transactions without a merchant',
           COUNT(*)
    FROM public.transactions t
    LEFT JOIN public.merchants m ON m.merchant_id = t.merchant_id
    WHERE m.merchant_id IS NULL

    UNION ALL

    SELECT 'batches without a merchant',
           COUNT(*)
    FROM public.batches b
    LEFT JOIN public.merchants m ON m.merchant_id = b.merchant_id
    WHERE m.merchant_id IS NULL

    UNION ALL

    SELECT 'settlements without a batch',
           COUNT(*)
    FROM public.settlements s
    LEFT JOIN public.batches b ON b.batch_id = s.batch_id
    WHERE b.batch_id IS NULL;
$$;

COMMENT ON FUNCTION settlement_svc.reconcile_orphans() IS
    'Replaces the foreign keys once these schemas become separate '
    'databases. Must return zero for every row. Written while the '
    'constraints still exist, so there is a known-good baseline.';
