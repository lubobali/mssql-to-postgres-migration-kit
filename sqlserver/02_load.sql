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

SET NOCOUNT ON;
GO

/* ─── merchants ───────────────────────────────────────────────────── */

DELETE FROM dbo.settlements;
DELETE FROM dbo.batches;
DELETE FROM dbo.transactions;
DELETE FROM dbo.merchants;
GO

SET IDENTITY_INSERT dbo.merchants ON;

BULK INSERT dbo.merchants
FROM '/tmp/merchants.psv'
WITH (
    FORMAT           = 'CSV',
    FIELDTERMINATOR  = '|',
    ROWTERMINATOR    = '0x0a',
    CODEPAGE         = '65001',   -- UTF-8, so the unicode names survive the load
    KEEPNULLS,
    TABLOCK
);

SET IDENTITY_INSERT dbo.merchants OFF;
GO

/* ─── transactions ────────────────────────────────────────────────── */

SET IDENTITY_INSERT dbo.transactions ON;

BULK INSERT dbo.transactions
FROM '/tmp/transactions.psv'
WITH (
    FORMAT           = 'CSV',
    FIELDTERMINATOR  = '|',
    ROWTERMINATOR    = '0x0a',
    CODEPAGE         = '65001',
    KEEPNULLS,
    TABLOCK,
    BATCHSIZE        = 50000
);

SET IDENTITY_INSERT dbo.transactions OFF;
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

   Both counts are identical on SQL Server because the default collation
   ignores case. Run the same two queries on PostgreSQL after migration
   and they will differ — with no error to warn you. */

SELECT
    'case-insensitive match (SQL Server default)' AS comparison,
    COUNT(*)                                      AS row_count
FROM dbo.transactions WHERE txn_status = 'Captured'
UNION ALL
SELECT
    'exact binary match',
    COUNT(*)
FROM dbo.transactions
WHERE CAST(txn_status AS VARBINARY(40)) = CAST('Captured' AS VARBINARY(40));
GO
