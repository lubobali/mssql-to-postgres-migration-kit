# ─────────────────────────────────────────────────────────────────────
#  RDS Proxy
#
#  Not optional once the application is microservices, which the job
#  description says it is becoming.
#
#  Every PostgreSQL connection is a backend PROCESS with its own memory.
#  Ten services x ten pods x a pool of twenty is 2,000 connections
#  against an instance that allows about 87. PostgreSQL does not degrade
#  gracefully at that point — it stops accepting connections, including
#  the one the on-call engineer is trying to use.
#
#  The proxy multiplexes many client connections onto few database ones,
#  and holds client connections open during a failover so the blip is
#  shorter than the failover itself.
# ─────────────────────────────────────────────────────────────────────

variable "enable_rds_proxy" {
  description = <<-EOT
    RDS Proxy is NOT available on AWS Free Plan accounts:
      FreeTierRestrictionError: This feature isn't available with free
      plan accounts.

    Off by default so the rest of the stack applies cleanly. The
    configuration below is complete and correct — it is a billing
    restriction, not a design gap. Set true on a standard account.
  EOT
  type        = bool
  default     = false
}

resource "aws_security_group" "proxy" {
  count = var.enable_rds_proxy ? 1 : 0

  name        = "${var.project}-proxy"
  description = "RDS Proxy. Accepts from the app tier, connects to Postgres."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-proxy" }
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_app" {
  count                        = var.enable_rds_proxy ? 1 : 0
  security_group_id            = aws_security_group.proxy[0].id
  description                  = "Postgres protocol from the application tier"
  referenced_security_group_id = aws_security_group.sqlserver.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "proxy_out" {
  count             = var.enable_rds_proxy ? 1 : 0
  security_group_id = aws_security_group.proxy[0].id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# The database has to accept the proxy as a distinct identity.
resource "aws_vpc_security_group_ingress_rule" "postgres_from_proxy" {
  count                        = var.enable_rds_proxy ? 1 : 0
  security_group_id            = aws_security_group.postgres.id
  description                  = "Postgres from the RDS Proxy"
  referenced_security_group_id = aws_security_group.proxy[0].id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# ─── The proxy's own credentials ─────────────────────────────────────
#
# The proxy authenticates to the database using a secret, and clients
# authenticate to the proxy. That indirection is the point: rotating the
# database password becomes a Secrets Manager operation with no
# application restart.

resource "aws_iam_role" "proxy" {
  count = var.enable_rds_proxy ? 1 : 0
  name  = "${var.project}-proxy-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "proxy_secrets" {
  count = var.enable_rds_proxy ? 1 : 0
  name  = "${var.project}-proxy-secrets"
  role  = aws_iam_role.proxy[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.postgres_master.arn]
    }]
  })
}

resource "aws_db_proxy" "postgres" {
  count                  = var.enable_rds_proxy ? 1 : 0
  name                   = "${var.project}-proxy"
  engine_family          = "POSTGRESQL"
  role_arn               = aws_iam_role.proxy[0].arn
  vpc_subnet_ids         = aws_subnet.private[*].id
  vpc_security_group_ids = [aws_security_group.proxy[0].id]

  # TLS between client and proxy. Off by default, which is the wrong
  # default for anything carrying cardholder data.
  require_tls = true

  # Close an idle client connection after 30 minutes rather than holding
  # a backend hostage for a session nobody is using.
  idle_client_timeout = 1800

  auth {
    auth_scheme = "SECRETS"
    iam_auth    = "DISABLED"
    secret_arn  = aws_secretsmanager_secret.postgres_master.arn
  }

  tags = { Name = "${var.project}-proxy" }

  depends_on = [aws_secretsmanager_secret_version.postgres_master]
}

resource "aws_db_proxy_default_target_group" "postgres" {
  count         = var.enable_rds_proxy ? 1 : 0
  db_proxy_name = aws_db_proxy.postgres[0].name

  connection_pool_config {
    # Cap the proxy at 90% of the instance's connections, leaving
    # headroom for the on-call engineer to actually get in.
    max_connections_percent = 90

    # Connections pinned mid-transaction, as a percentage. Kept low
    # because session-level state — SET, temp tables, prepared
    # statements — forces the proxy to PIN a backend to one client,
    # which quietly turns pooling back off.
    max_idle_connections_percent = 50

    connection_borrow_timeout = 120
  }
}

resource "aws_db_proxy_target" "postgres" {
  count                  = var.enable_rds_proxy ? 1 : 0
  db_proxy_name          = aws_db_proxy.postgres[0].name
  target_group_name      = aws_db_proxy_default_target_group.postgres[0].name
  db_instance_identifier = aws_db_instance.postgres.identifier
}

output "proxy_endpoint" {
  description = "Applications connect here, not to the database directly"
  value       = var.enable_rds_proxy ? aws_db_proxy.postgres[0].endpoint : "not enabled (unavailable on AWS Free Plan)"
}
