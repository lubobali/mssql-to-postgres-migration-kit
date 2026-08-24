# Outputs are how the scripts find the infrastructure without anything
# being hardcoded. No endpoint or password is ever typed into a file.

output "sqlserver_instance_id" {
  description = "Connect with: aws ssm start-session --target <this>"
  value       = aws_instance.sqlserver.id
}

output "sqlserver_private_ip" {
  description = "What DMS and Postgres see. Not reachable from outside the VPC."
  value       = aws_instance.sqlserver.private_ip
}

output "postgres_endpoint" {
  description = "RDS hostname. Only resolvable and reachable from inside the VPC."
  value       = aws_db_instance.postgres.address
}

output "postgres_port" {
  value = aws_db_instance.postgres.port
}

output "postgres_database" {
  value = aws_db_instance.postgres.db_name
}

output "secret_sqlserver_sa" {
  description = "Secrets Manager name holding the SQL Server SA credential"
  value       = aws_secretsmanager_secret.sqlserver_sa.name
}

output "secret_postgres_master" {
  description = "Secrets Manager name holding the Postgres master credential"
  value       = aws_secretsmanager_secret.postgres_master.name
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "Where DMS gets placed"
  value       = aws_subnet.private[*].id
}

output "dms_security_group_id" {
  value = aws_security_group.dms.id
}

# Printed after apply so the next step is never a guess.
output "next_step" {
  value = <<-EOT

    Shell on the SQL Server host (no SSH key, no open port):
      aws ssm start-session --target ${aws_instance.sqlserver.id}

    Read a secret:
      aws secretsmanager get-secret-value \
        --secret-id ${aws_secretsmanager_secret.postgres_master.name} \
        --query SecretString --output text | jq -r .password

    Postgres lives at ${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}
    and is reachable ONLY from inside the VPC.
  EOT
}
