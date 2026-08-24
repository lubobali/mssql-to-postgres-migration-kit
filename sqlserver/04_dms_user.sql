/* ═══════════════════════════════════════════════════════════════════
   A dedicated login for DMS — and the reason is not only security.

   DMS has two ways to read changes from SQL Server:

     MS-REPLICATION  needs a configured Distributor. Heavyweight.
     MS-CDC          needs CDC enabled on the database and tables.

   And the choice is not yours to make directly. DMS decides based on
   the PRIVILEGES OF THE ACCOUNT IT CONNECTS WITH:

     sysadmin      -> MS-REPLICATION
     not sysadmin  -> MS-CDC

   Connecting as sa therefore forces MS-REPLICATION, and the task dies
   with "The Distributor has not been installed correctly" no matter
   what setUpMsCdcForTables is set to. The attribute cannot override the
   decision, because the decision was already made from the login.

   So the fix for the error and the fix for the security problem are the
   same fix. DMS should never have been connecting as sa.
   ══════════════════════════════════════════════════════════════════ */

USE master;
GO

SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

DECLARE @pw NVARCHAR(128) = '$(DMS_PASSWORD)';

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'dms_user')
BEGIN
    EXEC('CREATE LOGIN dms_user WITH PASSWORD = ''' + @pw + ''', CHECK_POLICY = OFF');
    PRINT 'created login dms_user';
END
ELSE
BEGIN
    EXEC('ALTER LOGIN dms_user WITH PASSWORD = ''' + @pw + '''');
    PRINT 'login dms_user already existed; password rotated';
END
GO

/* VIEW SERVER STATE lets DMS read sys.dm_* — how it inspects the log
   position. A server-level grant, and the only one needed. */
GRANT VIEW SERVER STATE TO dms_user;
GO

/* msdb: DMS reads the CDC capture job's schedule and history to know
   whether changes are actually being captured. Without this the task
   starts and then silently reads nothing. */
USE msdb;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'dms_user')
    CREATE USER dms_user FOR LOGIN dms_user;
GRANT SELECT ON msdb.dbo.sysjobs      TO dms_user;
GRANT SELECT ON msdb.dbo.sysjobsteps  TO dms_user;
GRANT SELECT ON msdb.dbo.sysjobhistory TO dms_user;
GO

/* The source database.

   db_owner is what AWS documents for MS-CDC, and it is more than strictly
   needed — DMS reads the cdc.* tables and the source tables, it does not
   need to alter the schema. Narrowing it further is possible and is the
   kind of thing a PCI audit asks about; db_owner here matches the
   documented configuration, and the gap is worth naming rather than
   leaving implied. */
USE payments;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'dms_user')
    CREATE USER dms_user FOR LOGIN dms_user;
ALTER ROLE db_owner ADD MEMBER dms_user;
GO

/* Prove it is NOT sysadmin. If this returns 1, DMS will choose
   MS-REPLICATION and the task will fail exactly as before. */
SELECT
    'dms_user'                                          AS login_name,
    IS_SRVROLEMEMBER('sysadmin', 'dms_user')            AS is_sysadmin,
    CASE IS_SRVROLEMEMBER('sysadmin', 'dms_user')
        WHEN 1 THEN 'WRONG — DMS will use MS-REPLICATION and fail'
        ELSE 'correct — DMS will use MS-CDC'
    END                                                 AS verdict;
GO
