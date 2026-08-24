# ─────────────────────────────────────────────────────────────────────
#  The target: RDS PostgreSQL 15
#
#  Encrypted, private, with the extensions the project needs loaded at
#  the parameter-group level — because pg_cron and pg_stat_statements
#  both require shared_preload_libraries, which means a reboot. Getting
#  that wrong costs you a restart mid-migration.
# ─────────────────────────────────────────────────────────────────────

resource "aws_db_parameter_group" "postgres" {
  name        = "${var.project}-pg15"
  family      = "postgres15"
  description = "pg_cron for scheduled jobs, pg_stat_statements for query tuning"

  # RDS Postgres has no SQL Server Agent. pg_cron is how a nightly job
  # gets rebuilt after the migration — one of the things people forget
  # once the tables are across.
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_cron,pg_stat_statements"
    apply_method = "pending-reboot"
  }

  # pg_cron must live in a named database; it cannot float.
  parameter {
    name         = "cron.database_name"
    value        = "payments"
    apply_method = "pending-reboot"
  }

  # Log any statement slower than 1 second. Without this you are guessing
  # about performance instead of measuring it.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "postgres" {
  identifier = "${var.project}-pg"

  engine         = "postgres"
  engine_version = var.postgres_version
  instance_class = var.postgres_instance_class

  db_name  = "payments"
  username = "pgadmin" # "admin" and "postgres" are both reserved by RDS
  password = random_password.postgres_master.result

  allocated_storage     = 20
  max_allocated_storage = 100 # storage autoscaling, so a big load cannot fill the disk
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name

  # Not reachable from the internet. Full stop.
  publicly_accessible = false

  multi_az = var.multi_az

  # 7 days of backups also enables point-in-time recovery. Zero would
  # disable PITR entirely, and PITR is half of what "recoverability
  # meets SLA" actually means.
  backup_retention_period = 7
  backup_window           = "07:00-08:00" # UTC, off-peak for US business hours
  maintenance_window      = "sun:08:00-sun:09:00"

  # Query-level performance data. The free tier is 7 days of retention,
  # which is plenty to answer "why was it slow yesterday".
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  # IAM database authentication: an application can connect with a
  # short-lived IAM token instead of a stored password.
  iam_database_authentication_enabled = true

  auto_minor_version_upgrade = true
  deletion_protection        = false # lab only — production would be true
  skip_final_snapshot        = true  # lab only — production would never
  apply_immediately          = true

  tags = {
    Name = "${var.project}-postgres"
    role = "migration-target"
  }
}
