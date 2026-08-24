# ─────────────────────────────────────────────────────────────────────
#  Secrets
#
#  No password is ever typed, committed, or passed on a command line.
#  Terraform generates them, Secrets Manager stores them, and the EC2
#  instance reads them at runtime through its IAM role.
#
#  Note: generated passwords DO land in terraform.tfstate in plaintext.
#  That is why .gitignore blocks tfstate, and why a real deployment puts
#  state in an encrypted S3 backend rather than on a laptop.
# ─────────────────────────────────────────────────────────────────────

# SQL Server rejects passwords that do not meet its complexity policy,
# and chokes on some special characters when passed through Docker env
# vars. This character set is deliberately conservative.
resource "random_password" "sqlserver_sa" {
  length           = 32
  special          = true
  override_special = "!#%*-_=+"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2
}

resource "random_password" "postgres_master" {
  length           = 32
  special          = true
  override_special = "!#%*-_=+"
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 2
}

# recovery_window_in_days = 0 means an immediate hard delete on destroy.
# The AWS default is a 30-day recovery window, which would block you from
# re-creating a secret with the same name — painful in a lab you tear
# down and rebuild. Never do this in production.

resource "aws_secretsmanager_secret" "sqlserver_sa" {
  name                    = "${var.project}/sqlserver/sa"
  description             = "SA password for the legacy SQL Server container"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "sqlserver_sa" {
  secret_id = aws_secretsmanager_secret.sqlserver_sa.id
  secret_string = jsonencode({
    username = "sa"
    password = random_password.sqlserver_sa.result
    engine   = "sqlserver"
    port     = 1433
  })
}

resource "aws_secretsmanager_secret" "postgres_master" {
  name                    = "${var.project}/postgres/master"
  description             = "Master credential for the RDS PostgreSQL instance"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "postgres_master" {
  secret_id = aws_secretsmanager_secret.postgres_master.id
  secret_string = jsonencode({
    username = aws_db_instance.postgres.username
    password = random_password.postgres_master.result
    engine   = "postgres"
    host     = aws_db_instance.postgres.address
    port     = aws_db_instance.postgres.port
    dbname   = aws_db_instance.postgres.db_name
  })
}
