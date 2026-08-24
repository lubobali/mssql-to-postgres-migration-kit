# ─────────────────────────────────────────────────────────────────────
#  Security groups
#
#  The rule that matters: the Postgres security group does not accept a
#  CIDR range for the database port. It accepts only *other security
#  groups*. That means "the SQL Server host can reach Postgres" is
#  expressed as an identity, not as an IP address that might change or
#  be spoofed.
#
#  Nothing on the internet can open a connection to the database. Not
#  from Lubo's laptop, not from anywhere.
# ─────────────────────────────────────────────────────────────────────

# ─── SQL Server host ─────────────────────────────────────────────────

resource "aws_security_group" "sqlserver" {
  name        = "${var.project}-sqlserver"
  description = "Legacy SQL Server host. No inbound from the internet."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project}-sqlserver"
  }
}

# Deliberately NO inbound rules.
#
# Access is via SSM Session Manager, which works by the instance opening
# an outbound connection to AWS. Port 22 stays closed to the entire
# internet, which removes the single most attacked surface on any EC2
# instance. (The last production compromise Lubo dealt with started as
# an SSH brute force.)

resource "aws_vpc_security_group_egress_rule" "sqlserver_all" {
  security_group_id = aws_security_group.sqlserver.id
  description       = "Outbound for SSM agent, yum, and docker pulls"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ─── PostgreSQL ──────────────────────────────────────────────────────

resource "aws_security_group" "postgres" {
  name        = "${var.project}-postgres"
  description = "RDS PostgreSQL. Reachable only from inside this VPC."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project}-postgres"
  }
}

resource "aws_vpc_security_group_ingress_rule" "postgres_from_sqlserver" {
  security_group_id = aws_security_group.postgres.id
  description       = "Postgres from the SQL Server host only"

  referenced_security_group_id = aws_security_group.sqlserver.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "postgres_from_dms" {
  security_group_id = aws_security_group.postgres.id
  description       = "Postgres from the DMS replication instance"

  referenced_security_group_id = aws_security_group.dms.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# ─── DMS replication instance ────────────────────────────────────────

resource "aws_security_group" "dms" {
  name        = "${var.project}-dms"
  description = "DMS replication instance. Needs to reach both databases."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project}-dms"
  }
}

resource "aws_vpc_security_group_egress_rule" "dms_all" {
  security_group_id = aws_security_group.dms.id
  description       = "Outbound to source and target databases"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# DMS reaching SQL Server on 1433
resource "aws_vpc_security_group_ingress_rule" "sqlserver_from_dms" {
  security_group_id = aws_security_group.sqlserver.id
  description       = "SQL Server from the DMS replication instance"

  referenced_security_group_id = aws_security_group.dms.id
  from_port                    = 1433
  to_port                      = 1433
  ip_protocol                  = "tcp"
}
