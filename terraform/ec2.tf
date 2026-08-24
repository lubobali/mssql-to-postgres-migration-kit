# ─────────────────────────────────────────────────────────────────────
#  The legacy system: SQL Server 2022 on EC2
#
#  Developer Edition is free for non-production use, and SQL Server has
#  run on Linux since 2017 — so this is Docker on Amazon Linux, not a
#  Windows licence.
#
#  Access is via SSM Session Manager. No key pair, no port 22, nothing
#  inbound at all.
# ─────────────────────────────────────────────────────────────────────

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# ─── IAM: let the instance talk to Session Manager and Secrets Manager ─

resource "aws_iam_role" "sqlserver" {
  name = "${var.project}-sqlserver-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# AWS-managed policy. Everything Session Manager needs, nothing more.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.sqlserver.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Read exactly two secrets. Not "secretsmanager:*" — this is what least
# privilege actually looks like when you write it down.
resource "aws_iam_role_policy" "read_secrets" {
  name = "${var.project}-read-secrets"
  role = aws_iam_role.sqlserver.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = [
        aws_secretsmanager_secret.sqlserver_sa.arn,
        aws_secretsmanager_secret.postgres_master.arn,
      ]
    }]
  })
}

resource "aws_iam_instance_profile" "sqlserver" {
  name = "${var.project}-sqlserver-profile"
  role = aws_iam_role.sqlserver.name
}

# ─── The instance ────────────────────────────────────────────────────

resource "aws_instance" "sqlserver" {
  ami           = data.aws_ssm_parameter.al2023.value
  instance_type = var.sqlserver_instance_type
  subnet_id     = aws_subnet.public.id

  vpc_security_group_ids = [aws_security_group.sqlserver.id]
  iam_instance_profile   = aws_iam_instance_profile.sqlserver.name

  # No key_name. There is no SSH key because there is no SSH.

  root_block_device {
    volume_size = 40
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only — blocks the SSRF class of attack
  }

  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail

    dnf update -y
    dnf install -y docker postgresql15 jq
    systemctl enable --now docker
    usermod -aG docker ec2-user

    # Python for the migration and verification scripts
    dnf install -y python3 python3-pip
  EOF

  # Re-running user_data on every change would be surprising. This makes
  # the instance replace only when the script actually changes.
  user_data_replace_on_change = true

  tags = {
    Name = "${var.project}-sqlserver"
    role = "legacy-source"
  }
}
