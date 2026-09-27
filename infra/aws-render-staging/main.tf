data "aws_caller_identity" "current" {}

locals {
  bucket_name = coalesce(
    var.bucket_name,
    "katcha-render-staging-${var.expected_account_id}-${var.aws_region}",
  )
}

resource "aws_s3_bucket" "render_staging" {
  bucket        = local.bucket_name
  force_destroy = false

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.expected_account_id
      error_message = "Refusing to manage staging resources in an unexpected AWS account."
    }
  }
}

resource "aws_s3_bucket_public_access_block" "render_staging" {
  bucket = aws_s3_bucket.render_staging.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "render_staging" {
  bucket = aws_s3_bucket.render_staging.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "render_staging" {
  bucket = aws_s3_bucket.render_staging.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "render_staging" {
  bucket = aws_s3_bucket.render_staging.id

  rule {
    id     = "expire-katcha-render-staging"
    status = "Enabled"

    filter {
      prefix = "${trim(var.staging_prefix, "/")}/"
    }

    expiration {
      days = var.expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "secure_transport" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.render_staging.arn,
      "${aws_s3_bucket.render_staging.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "secure_transport" {
  bucket = aws_s3_bucket.render_staging.id
  policy = data.aws_iam_policy_document.secure_transport.json

  depends_on = [aws_s3_bucket_public_access_block.render_staging]
}

data "aws_iam_policy_document" "renderer_access" {
  statement {
    sid    = "ListStagingPrefix"
    effect = "Allow"

    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucket",
    ]

    resources = [aws_s3_bucket.render_staging.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${trim(var.staging_prefix, "/")}/*"]
    }
  }

  statement {
    sid    = "ReadWriteStagedObjects"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]

    resources = [
      "${aws_s3_bucket.render_staging.arn}/${trim(var.staging_prefix, "/")}/*",
    ]
  }
}
