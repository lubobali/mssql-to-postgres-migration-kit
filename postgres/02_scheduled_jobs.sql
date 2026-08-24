/* ═══════════════════════════════════════════════════════════════════
   Rebuilding the scheduled job.

   This is the step people forget once the tables are across, and it is
   invisible until the night the report does not arrive.

   RDS PostgreSQL has NO SQL Server Agent. There is no job scheduler in
   the database, no job history table, no failure alerting, and nothing
   in a schema comparison will tell you a job is missing — because a job
   was never part of the schema.

   Three ways to replace it, and the choice matters:

     pg_cron              Lives in the database. Simplest thing that
                          works. On RDS it needs pg_cron in
                          shared_preload_libraries and a reboot, and
                          cron.database_name must name the database.
                          Runs on the writer only, which is correct here
                          and a trap on a Multi-AZ failover if the job
                          assumes it is the only one running.

     Lambda + EventBridge Lives outside the database. Better observability
                          (CloudWatch metrics, alarms, retries, DLQ) and
                          survives a database restart. More moving parts
                          and another IAM boundary to get right.

     Airflow / MWAA       Right answer when the job has dependencies, or
                          when it is one step of a pipeline rather than a
                          standalone task. Heavy for a single nightly
                          summary.

   pg_cron here, because the job is self-contained, has no upstream
   dependency, and touches nothing outside the database it lives in.
   ═══════════════════════════════════════════════════════════════════ */

/* ─── what the job writes into ───────────────────────────────────── */

CREATE TABLE IF NOT EXISTS settlement_summary_daily (
    summary_date   date          NOT NULL,
    merchant_id    integer       NOT NULL,
    txn_count      bigint        NOT NULL,
    gross_amount   numeric(19,4) NOT NULL,
    total_fees     numeric(19,4) NOT NULL,
    net_amount     numeric(19,4) NOT NULL,
    generated_at   timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (summary_date, merchant_id)
);

/* ─── and a record of every run ──────────────────────────────────── */

/* SQL Server Agent keeps run history for free — msdb.dbo.sysjobhistory.
   pg_cron keeps cron.job_run_details, but only if
   cron.log_run is on, and it is not retained forever.

   So the job records its own outcome. "Did last night's job run?" has to
   be answerable without reading logs, or nobody will ask it until a
   customer does. */

CREATE TABLE IF NOT EXISTS job_run_log (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name     text        NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    rows_written integer,
    status       text        NOT NULL DEFAULT 'running',
    error        text
);

/* ─── the job itself ─────────────────────────────────────────────── */

CREATE OR REPLACE FUNCTION run_daily_settlement_summary(days_back integer DEFAULT 1)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    v_run_id bigint;
    v_rows   integer;
BEGIN
    INSERT INTO job_run_log (job_name) VALUES ('daily_settlement_summary')
    RETURNING run_id INTO v_run_id;

    -- Idempotent by upsert. A scheduled job WILL be run twice — a retry,
    -- a manual re-run after an incident, a failover replaying it. If
    -- running it twice doubles the numbers, the job is a liability.
    INSERT INTO settlement_summary_daily
        (summary_date, merchant_id, txn_count, gross_amount, total_fees, net_amount)
    SELECT
        t.captured_at::date,
        t.merchant_id,
        COUNT(*),
        SUM(t.amount),
        SUM(COALESCE(t.fee_amount, 0)),
        SUM(t.amount - COALESCE(t.fee_amount, 0))
    FROM transactions t
    WHERE t.captured_at >= (now() - make_interval(days => days_back))::date
      -- lower(), not = 'Captured'.
      --
      -- This is the collation remediation, applied deliberately at the
      -- one place it is needed rather than by quietly rewriting the data.
      -- The legacy query matched all three case variants because SQL
      -- Server's collation ignored case; a literal port to PostgreSQL
      -- would silently drop two thirds of the rows. See docs/FINDINGS.md.
      AND lower(t.txn_status) = 'captured'
    GROUP BY t.captured_at::date, t.merchant_id
    ON CONFLICT (summary_date, merchant_id) DO UPDATE SET
        txn_count    = EXCLUDED.txn_count,
        gross_amount = EXCLUDED.gross_amount,
        total_fees   = EXCLUDED.total_fees,
        net_amount   = EXCLUDED.net_amount,
        generated_at = now();

    GET DIAGNOSTICS v_rows = ROW_COUNT;

    UPDATE job_run_log
       SET finished_at = now(), rows_written = v_rows, status = 'succeeded'
     WHERE run_id = v_run_id;

    RETURN v_rows;

EXCEPTION WHEN OTHERS THEN
    -- Record the failure, then re-raise. Swallowing it would leave a job
    -- that "never fails" and never works.
    UPDATE job_run_log
       SET finished_at = now(), status = 'failed', error = SQLERRM
     WHERE run_id = v_run_id;
    RAISE;
END;
$$;

/* An index supporting the job's own WHERE clause.

   lower(txn_status) is not sargable against the plain btree on
   txn_status, so without this the nightly job sequentially scans the
   whole table. A functional index makes the remediation free. */

CREATE INDEX IF NOT EXISTS ix_txn_status_lower
    ON transactions (lower(txn_status), captured_at);

/* ─── schedule it ────────────────────────────────────────────────── */

DO $sched$
BEGIN
    PERFORM cron.unschedule('daily_settlement_summary');
EXCEPTION WHEN OTHERS THEN
    NULL;  -- not scheduled yet, or pg_cron unavailable
END
$sched$;

DO $sched$
BEGIN
    -- 06:15 UTC. pg_cron schedules in UTC regardless of the server's
    -- timezone, which is one more place a settlement date can land on
    -- the wrong day if somebody assumes local time.
    PERFORM cron.schedule(
        'daily_settlement_summary',
        '15 6 * * *',
        $job$SELECT run_daily_settlement_summary(1)$job$
    );
    RAISE NOTICE 'scheduled daily_settlement_summary at 06:15 UTC';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_cron unavailable (%) — the job exists but is not scheduled', SQLERRM;
END
$sched$;
