/* ═══════════════════════════════════════════════════════════════════
   Bulk load, then derive batches and settlements from the transactions.

   IDENTITY_INSERT is enabled deliberately. The generated files carry
   explicit primary keys so that the source data is reproducible across
   runs — which is the only way a verification baseline means anything.

   Turning it off again afterwards leaves the sequence sitting at the
   wrong high-water mark, which is TRAP 1 from the schema and shows up
   on the PostgreSQL side as a primary key collision on first insert.
   ═══════════════════════════════════════════════════════════════════ */

USE payments;
GO

/* Required for any statement touching a table with a computed column —
   including a plain DELETE. sqlcmd does not set it, and the error names
   the SET option rather than the table that needs it. */
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
SET NOCOUNT ON;
GO

/* ─── merchants ───────────────────────────────────────────────────── */

DELETE FROM dbo.settlements;
DELETE FROM dbo.batches;
DELETE FROM dbo.transactions;
DELETE FROM dbo.merchants;
GO

BULK INSERT dbo.merchants
FROM '/tmp/merchants.psv'
WITH (
    -- No FORMAT = 'CSV' here. That option cannot be combined with
    -- DATAFILETYPE, and it is unnecessary anyway: the generator writes
    -- unquoted fields with a pipe delimiter precisely so no quoting
    -- rules are involved.
    DATAFILETYPE     = 'widechar',
    FIELDTERMINATOR  = '|',
    ROWTERMINATOR    = '\n',
    KEEPIDENTITY,
    KEEPNULLS,
    TABLOCK
);

GO

/* ─── transactions ────────────────────────────────────────────────── */

BULK INSERT dbo.transactions
FROM '/tmp/transactions.psv'
WITH (
    FORMAT           = 'CSV',
    FIELDTERMINATOR  = '|',
    ROWTERMINATOR    = '0x0a',
    -- CODEPAGE unsupported on Linux; see note above
    KEEPIDENTITY,
    KEEPNULLS,
    TABLOCK,
    BATCHSIZE        = 50000
);

GO

/* ─── batches, derived ────────────────────────────────────────────── */

/* Only 'Captured' matches here, and the source collation is case-
   INSENSITIVE, so this picks up 'captured' and 'CAPTURED' too.

   The identical query on PostgreSQL will not. That difference is the
   collation trap, and it is worth seeing in a number rather than in
   a paragraph. */

INSERT INTO dbo.batches (merchant_id, batch_date, txn_count, gross_amount, batch_status)
SELECT
    t.merchant_id,
    CAST(t.captured_at AS DATE),
    COUNT(*),
    SUM(t.amount),
    CASE WHEN COUNT(t.settled_at) = COUNT(*) THEN 'Settled' ELSE 'Pending' END
FROM dbo.transactions t
WHERE t.txn_status = 'Captured'
GROUP BY t.merchant_id, CAST(t.captured_at AS DATE);
GO

/* ─── settlements, derived ────────────────────────────────────────── */

INSERT INTO dbo.settlements (batch_id, net_amount, fee_amount, settled_at)
SELECT
    b.batch_id,
    b.gross_amount * 0.9705,
    b.gross_amount * 0.0295,
    DATEADD(day, 2, CAST(b.batch_date AS DATETIME2(3)))
FROM dbo.batches b
WHERE b.batch_status = 'Settled';
GO

/* ─── what landed ─────────────────────────────────────────────────── */

SELECT 'merchants'    AS table_name, COUNT(*) AS row_count FROM dbo.merchants
UNION ALL SELECT 'transactions', COUNT(*) FROM dbo.transactions
UNION ALL SELECT 'batches',      COUNT(*) FROM dbo.batches
UNION ALL SELECT 'settlements',  COUNT(*) FROM dbo.settlements;
GO

/* The collation trap, as a number.

   The database collation is SQL_Latin1_General_CP1_CI_AS — case
   INsensitive. So the first count matches 'Captured', 'captured' and
   'CAPTURED' alike.

   The second forces a case-SENSITIVE collation, which is how PostgreSQL
   behaves by default. The gap between these two numbers is how many rows
   a migrated query silently stops returning.

   Nothing errors. The report just gets quieter. */

SELECT
    'case-insensitive (SQL Server default collation)' AS comparison,
    COUNT(*)                                          AS row_count
FROM dbo.transactions
WHERE txn_status = 'Captured'
UNION ALL
SELECT
    'case-sensitive (how PostgreSQL will behave)',
    COUNT(*)
FROM dbo.transactions
WHERE txn_status COLLATE SQL_Latin1_General_CP1_CS_AS = 'Captured';
GO
