variable "aws_region" {
  description = "AWS region used by the Remotion Lambda deployment and staging bucket."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Globally unique private S3 bucket name used only for temporary render inputs."
  type        = string

  validation {
    condition = (
      length(var.bucket_name) >= 3
      && length(var.bucket_name) <= 63
      && can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.bucket_name))
      && !strcontains(var.bucket_name, "..")
    )
    error_message = "bucket_name must be a valid lowercase S3 bucket name."
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
