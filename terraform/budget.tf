# ─────────────────────────────────────────────────────────────────────
#  Cost guardrail
#
#  This exists before any billable resource does, deliberately.
#
#  The failure mode with a lab account is not overspending on purpose —
#  it is forgetting a resource for three weeks. A budget alarm turns a
#  silent $50 surprise into an email on day two.
#
#  Scoped by tag, so it watches only this project and ignores the
#  existing job-streamer infrastructure.
# ─────────────────────────────────────────────────────────────────────

variable "alert_email" {
  description = "Where budget alerts go."
  type        = string
}

resource "aws_budgets_budget" "lab" {
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = "20"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:project$${var.project}"]
  }

  # Warn at 50% of actual spend — early enough to act.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  # And warn on the forecast, which catches a resource left running
  # before the money is actually spent.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
