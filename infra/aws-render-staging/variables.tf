variable "aws_region" {
  description = "AWS region used by the Remotion Lambda deployment and staging bucket."
  type        = string
  default     = "us-east-1"
}

variable "expected_account_id" {
  description = "The exact 12-digit AWS account ID where Katcha cloud rendering is allowed."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be exactly 12 digits."
  }
}

variable "bucket_name" {
  description = "Optional globally unique private S3 bucket name. Defaults to an account+region-scoped Katcha name."
  type        = string
  default     = null

  validation {
    condition = (
      var.bucket_name == null
      || (
        length(var.bucket_name) >= 3
        && length(var.bucket_name) <= 63
        && can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.bucket_name))
        && !strcontains(var.bucket_name, "..")
      )
    )
    error_message = "bucket_name must be null or a valid lowercase S3 bucket name."
  }
}

variable "staging_prefix" {
  description = "Object prefix reserved for temporary Katcha render inputs."
  type        = string
  default     = "katcha-render-staging"
}

variable "expiration_days" {
  description = "Days before staged render inputs are automatically expired by S3."
  type        = number
  default     = 1

  validation {
    condition     = var.expiration_days >= 1 && floor(var.expiration_days) == var.expiration_days
    error_message = "expiration_days must be an integer of at least 1."
  }
}
