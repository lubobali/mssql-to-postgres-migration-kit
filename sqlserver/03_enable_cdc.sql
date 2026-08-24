/* ═══════════════════════════════════════════════════════════════════
   Enable Change Data Capture, so DMS can do CDC.

   Full load alone means the source has to stop accepting writes for the
   whole migration AND the whole verification. At real volume that is an
   outage measured in hours.

   CDC means DMS copies what exists and then keeps applying changes as
   they happen. The source stays live. Writes freeze for the minutes it
   takes to verify a static target, not the hours it takes to move data.

   ──────────────────────────────────────────────────────────────────
   The irony worth noticing:

   CDC on SQL Server is implemented as SQL Server AGENT JOBS. Enabling
   it creates cdc.capture and cdc.cleanup jobs, and without the Agent
   running the capture tables stay empty while everything reports
   success.

   So the Agent — the thing PostgreSQL has no equivalent of, the thing
   this whole migration has to replace with pg_cron — is also the thing
   that has to be switched ON before the migration can happen at all.
   ══════════════════════════════════════════════════════════════════ */

USE payments;
GO

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

/* ─── Is the Agent actually running? ──────────────────────────────── */

IF NOT EXISTS (
    SELECT 1 FROM sys.dm_server_services
    WHERE servicename LIKE 'SQL Server Agent%' AND status_desc = 'Running'
)
BEGIN
    RAISERROR(
        'SQL Server Agent is not running. CDC capture jobs will never fire and the capture tables will stay empty while everything reports success. Start the container with MSSQL_AGENT_ENABLED=true.',
        16, 1);
END
GO

/* ─── Database level ──────────────────────────────────────────────── */

IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name = 'payments' AND is_cdc_enabled = 1)
BEGIN
    EXEC sys.sp_cdc_enable_db;
    PRINT 'CDC enabled on the payments database';
END
ELSE
    PRINT 'CDC already enabled on the payments database';
GO

/* ─── Per table ───────────────────────────────────────────────────── */

/* CDC is opt-in per table. A table added later without this is
   invisible to CDC, and DMS will full-load it and then silently never
   see another change to it — a gap that only appears after cutover. */

DECLARE @tables TABLE (name SYSNAME);
INSERT INTO @tables VALUES ('merchants'), ('transactions'), ('batches'), ('settlements');

DECLARE @t SYSNAME;
DECLARE c CURSOR FOR SELECT name FROM @tables;
OPEN c;
FETCH NEXT FROM c INTO @t;

WHILE @@FETCH_STATUS = 0
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM sys.tables
        WHERE name = @t AND schema_id = SCHEMA_ID('dbo') AND is_tracked_by_cdc = 1
    )
    BEGIN
        EXEC sys.sp_cdc_enable_table
            @source_schema        = N'dbo',
            @source_name          = @t,
            -- NULL means no gating role: any user who can read the table
            -- can read its change data. Correct for a migration service
            -- account; in production this would name a role, because CDC
            -- tables contain the same cardholder data as the source.
            @role_name            = NULL,
            @supports_net_changes = 0;
        PRINT 'CDC enabled on dbo.' + @t;
    END
    ELSE
        PRINT 'CDC already enabled on dbo.' + @t;

    FETCH NEXT FROM c INTO @t;
END

CLOSE c;
DEALLOCATE c;
GO

/* ─── What now exists ─────────────────────────────────────────────── */

SELECT
    s.name AS [schema],
    t.name AS [table],
    t.is_tracked_by_cdc
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name = 'dbo'
ORDER BY t.name;
GO

/* The capture and cleanup jobs CDC just created. These are Agent jobs,
   and they are why the Agent has to be running. */

SELECT job_id, name
FROM msdb.dbo.sysjobs
WHERE name LIKE 'cdc.%';
GO
