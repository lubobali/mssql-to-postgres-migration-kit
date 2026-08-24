# ─────────────────────────────────────────────────────────────────────
#  AWS Database Migration Service
#
#  The manual path in migration/migrate.py is how you learn what is
#  happening. This is what a real cutover uses, and the difference is
#  not speed — it is CDC.
#
#  A one-shot copy means the source must stop accepting writes for the
#  entire duration of the migration AND the verification. At any real
#  volume that is an outage measured in hours.
#
#  With full-load-and-cdc, DMS copies the existing rows and then keeps
#  applying changes as they happen. The source stays live and keeps
#  taking payments. Writes freeze for the minutes it takes to verify a
#  static target, not for the hours it takes to move the data.
#
#  That is the whole argument, and it is why "we used DMS" is a
#  statement about downtime rather than about tooling.
# ─────────────────────────────────────────────────────────────────────

# ─── The roles DMS requires by NAME ──────────────────────────────────
#
# These two are account-wide singletons with names AWS hardcodes. DMS
# looks them up by name, not by ARN, so they cannot be prefixed per
# project. Creating them here is correct for an account that has never
# run DMS, and would collide in one that has — which is worth knowing
# before running this against a shared account.

resource "aws_iam_role" "dms_vpc" {
  name = "dms-vpc-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dms.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { project = var.project }
}

resource "aws_iam_role_policy_attachment" "dms_vpc" {
  role       = aws_iam_role.dms_vpc.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonDMSVPCManagementRole"
}

resource "aws_iam_role" "dms_logs" {
  name = "dms-cloudwatch-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dms.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { project = var.project }
}

resource "aws_iam_role_policy_attachment" "dms_logs" {
  role       = aws_iam_role.dms_logs.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonDMSCloudWatchLogsRole"
}

# ─── Where the replication instance lives ────────────────────────────

resource "aws_dms_replication_subnet_group" "main" {
  replication_subnet_group_id = "${var.project}-dms-subnets"
  # ASCII only. DMS rejects this field with "must not contain
  # non-printable control characters" for anything outside it — an
  # em-dash is enough to fail the create.
  replication_subnet_group_description = "Private subnets. DMS reaches both databases without touching the internet."
  subnet_ids                           = aws_subnet.private[*].id

  tags = { Name = "${var.project}-dms-subnets" }

  depends_on = [aws_iam_role_policy_attachment.dms_vpc]
}

resource "aws_dms_replication_instance" "main" {
  replication_instance_id = "${var.project}-dms"
  # dms.t3.micro no longer exists — AWS retired it, and the API says only
  # "Invalid ReplicationInstance class" rather than naming what is valid.
  #   aws dms describe-orderable-replication-instances
  # is the authority. t3.small is the smallest that remains, at roughly
  # $0.036/hr.
  replication_instance_class = "dms.t3.small"

  # Sized for the change log, not for the data. DMS streams the full load
  # rather than staging it, so this only has to hold cached changes while
  # the full load runs.
  allocated_storage = 20

  replication_subnet_group_id = aws_dms_replication_subnet_group.main.replication_subnet_group_id
  vpc_security_group_ids      = [aws_security_group.dms.id]

  # Not publicly accessible. Both endpoints are inside this VPC, so
  # nothing about this replication ever traverses the internet — which
  # is the entire reason for putting the source on EC2 rather than
  # exposing a database to the world.
  publicly_accessible = false

  multi_az                   = false
  auto_minor_version_upgrade = true
  apply_immediately          = true

  tags = { Name = "${var.project}-dms" }

  depends_on = [
    aws_iam_role_policy_attachment.dms_vpc,
    aws_iam_role_policy_attachment.dms_logs,
  ]
}

# ─── Source: the legacy SQL Server ───────────────────────────────────

resource "aws_dms_endpoint" "source" {
  endpoint_id   = "${var.project}-source-mssql"
  endpoint_type = "source"
  engine_name   = "sqlserver"

  server_name   = aws_instance.sqlserver.private_ip
  port          = 1433
  database_name = "payments"
  username      = "sa"
  password      = random_password.sqlserver_sa.result

  ssl_mode = "none" # container uses a self-signed certificate

  # Two attributes, and the first one is not optional.
  #
  # setUpMsCdcForTables=true
  #   DMS has TWO ways to read changes from SQL Server, and it picks the
  #   wrong one by default here. MS-REPLICATION is the default and needs
  #   a configured Distributor — full transactional replication
  #   infrastructure. Without it the task dies at startup with:
  #
  #     "The MS SQL Server instance is not set up for Replication."
  #     "The Distributor has not been installed correctly. Could not
  #      enable database for publishing."
  #
  #   MS-CDC is the lighter path and the one this source has enabled
  #   (see sqlserver/03_enable_cdc.sql). This attribute is what tells
  #   DMS to use it. Nothing in the endpoint configuration hints that
  #   the choice exists.
  #
  # safeguardPolicy=RELY_ON_SQL_SERVER_REPLICATION_AGENT
  #   Governs how DMS stops the transaction log being truncated before
  #   it has read it. The default, EXCLUSIVE_AUTOMATIC_TRUNCATION, opens
  #   a transaction on the source to hold the log open — surprising
  #   behaviour on a production server. With MS-CDC the capture job
  #   already manages truncation, so DMS can rely on it instead of
  #   interfering.
  extra_connection_attributes = "setUpMsCdcForTables=true;safeguardPolicy=RELY_ON_SQL_SERVER_REPLICATION_AGENT"

  tags = { Name = "${var.project}-source" }
}

# ─── Target: RDS PostgreSQL ──────────────────────────────────────────

resource "aws_dms_endpoint" "target" {
  endpoint_id   = "${var.project}-target-pg"
  endpoint_type = "target"
  engine_name   = "postgres"

  server_name   = aws_db_instance.postgres.address
  port          = 5432
  database_name = aws_db_instance.postgres.db_name
  username      = aws_db_instance.postgres.username
  password      = random_password.postgres_master.result

  ssl_mode = "require"

  tags = { Name = "${var.project}-target" }
}

# ─── The task ────────────────────────────────────────────────────────

resource "aws_dms_replication_task" "migrate" {
  replication_task_id      = "${var.project}-task"
  replication_instance_arn = aws_dms_replication_instance.main.replication_instance_arn
  source_endpoint_arn      = aws_dms_endpoint.source.endpoint_arn
  target_endpoint_arn      = aws_dms_endpoint.target.endpoint_arn

  # full-load-and-cdc: copy what exists, then keep applying changes.
  # This is the only mode that lets the source stay live.
  migration_type = "full-load-and-cdc"

  table_mappings            = file("${path.module}/dms/table-mappings.json")
  replication_task_settings = file("${path.module}/dms/task-settings.json")

  # Started explicitly rather than on create, so the task can be
  # inspected before it touches anything.
  start_replication_task = false

  tags = { Name = "${var.project}-task" }

  lifecycle {
    # DMS rewrites these server-side with defaults filled in, so
    # Terraform sees a diff on every plan otherwise.
    ignore_changes = [replication_task_settings]
  }
}

output "dms_task_arn" {
  value = aws_dms_replication_task.migrate.replication_task_arn
}

output "dms_instance_arn" {
  value = aws_dms_replication_instance.main.replication_instance_arn
}
