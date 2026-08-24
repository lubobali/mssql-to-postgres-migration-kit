variable "region" {
  description = "AWS region. us-east-2 because that is where the existing account resources live."
  type        = string
  default     = "us-east-2"
}

variable "project" {
  description = "Name prefix for every resource, so teardown is unambiguous."
  type        = string
  default     = "rds-migration-lab"
}

variable "admin_cidr" {
  description = <<-EOT
    The single IP allowed to reach anything in this VPC from outside.
    A /32 — one address, not a range. Everything else on the internet
    cannot see these resources at all.
  EOT
  type        = string
}

variable "vpc_cidr" {
  description = "10.20.0.0/16 — deliberately far from the default VPC's 172.31.0.0/16 so nothing can collide with job-streamer."
  type        = string
  default     = "10.20.0.0/16"
}

variable "postgres_version" {
  description = "PostgreSQL engine version. The job description names 15.x specifically."
  type        = string
  default     = "15.19"
}

variable "postgres_instance_class" {
  description = "db.t4g.micro — about $0.016/hr. Graviton, cheapest class that supports Performance Insights."
  type        = string
  default     = "db.t4g.micro"
}

variable "sqlserver_instance_type" {
  description = <<-EOT
    t3.medium — 4 GB RAM. SQL Server needs 2 GB minimum and is miserable at
    exactly 2 GB. Must be x86: SQL Server does not run on Graviton.
  EOT
  type        = string
  default     = "t3.medium"
}

variable "multi_az" {
  description = "Multi-AZ doubles the RDS cost. Off by default; flip on to demonstrate failover, then off again."
  type        = bool
  default     = false
}
