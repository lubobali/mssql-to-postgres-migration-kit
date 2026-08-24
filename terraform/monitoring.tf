# ─────────────────────────────────────────────────────────────────────
#  Monitoring
#
#  Four alarms, each chosen because it precedes an outage rather than
#  describing one. A dashboard nobody watches is decoration; an alarm
#  that fires at 3am is the product.
#
#  Deliberately NOT alarming on CPU. High CPU on a database is usually
#  a symptom of something else, and a CPU alarm trains people to ignore
#  alarms.
# ─────────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ─── Storage. The one that actually takes databases down ─────────────

resource "aws_cloudwatch_metric_alarm" "free_storage" {
  alarm_name        = "${var.project}-free-storage-low"
  alarm_description = <<-EOT
    Free storage below 2 GB.

    A full disk stops a PostgreSQL instance accepting writes, and the
    recovery is not instant. Storage autoscaling is enabled, but this
    fires if it cannot keep up with a bulk load.
  EOT

  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 2 * 1024 * 1024 * 1024

  dimensions    = { DBInstanceIdentifier = aws_db_instance.postgres.identifier }
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  treat_missing_data = "breaching"
}

# ─── Connections. The microservices failure mode ─────────────────────

resource "aws_cloudwatch_metric_alarm" "connections" {
  alarm_name        = "${var.project}-connections-high"
  alarm_description = <<-EOT
    Connection count approaching the instance limit.

    db.t4g.micro allows roughly 87 connections. Every PostgreSQL
    connection is a backend process with its own memory, so the database
    does not degrade gracefully here — it stops accepting connections
    entirely. This is the alarm that matters once the application is
    microservices, and it is why RDS Proxy exists.
  EOT

  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 60

  dimensions    = { DBInstanceIdentifier = aws_db_instance.postgres.identifier }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ─── Replication slots and WAL. The quiet killer ─────────────────────

resource "aws_cloudwatch_metric_alarm" "transaction_logs" {
  alarm_name        = "${var.project}-transaction-log-growth"
  alarm_description = <<-EOT
    Transaction log disk usage above 4 GB.

    Relevant specifically because DMS uses logical replication. An
    abandoned replication slot — a DMS task deleted without dropping its
    slot — makes PostgreSQL retain WAL forever, and the disk fills with
    no query to blame. It is the classic way a migration takes down the
    database it already finished migrating.
  EOT

  namespace           = "AWS/RDS"
  metric_name         = "TransactionLogsDiskUsage"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 4 * 1024 * 1024 * 1024

  dimensions    = { DBInstanceIdentifier = aws_db_instance.postgres.identifier }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ─── Read latency ────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "read_latency" {
  alarm_name        = "${var.project}-read-latency-high"
  alarm_description = "Read latency above 100ms sustained — storage or a bad plan."

  namespace           = "AWS/RDS"
  metric_name         = "ReadLatency"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0.1

  dimensions    = { DBInstanceIdentifier = aws_db_instance.postgres.identifier }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ─── RDS events. Failovers and low storage, from the service itself ──

resource "aws_db_event_subscription" "postgres" {
  name      = "${var.project}-db-events"
  sns_topic = aws_sns_topic.alerts.arn

  source_type = "db-instance"
  source_ids  = [aws_db_instance.postgres.identifier]

  # failover: you want to know. availability: it went away and came back.
  # low storage: the service noticed before the metric alarm did.
  event_categories = ["availability", "failover", "failure", "low storage", "maintenance"]
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
