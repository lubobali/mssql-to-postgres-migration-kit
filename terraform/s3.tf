# ─────────────────────────────────────────────────────────────────────
#  Analytics export bucket — the Snowflake path
#
#  Snowflake reads Parquet from S3 natively through an external stage,
#  so this bucket IS the pipeline's delivery point. No Snowflake account
#  is needed to make the shape real.
# ─────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "analytics" {
  bucket        = "${var.project}-analytics-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # lab only — production would never

  tags = { Name = "${var.project}-analytics" }
}

# Block every form of public access. Four separate settings because AWS
# has four separate ways to accidentally make a bucket public.
resource "aws_s3_bucket_public_access_block" "analytics" {
  bucket                  = aws_s3_bucket.analytics.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  versioning_configuration { status = "Enabled" }
}

# Exported analytics files are derived data — they can always be
# regenerated from the database. Expiring old versions keeps a
# re-exported partition from accumulating copies forever.
resource "aws_s3_bucket_lifecycle_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

resource "aws_iam_role_policy" "write_analytics" {
  name = "${var.project}-write-analytics"
  role = aws_iam_role.sqlserver.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.analytics.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.analytics.arn
      },
    ]
  })
}

output "analytics_bucket" {
  value = aws_s3_bucket.analytics.bucket
}
