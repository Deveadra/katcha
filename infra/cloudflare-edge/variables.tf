variable "cloudflare_api_token" {
  description = "Cloudflare API token used only by Terraform to manage Access applications."
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID containing the Katcha production hostname."
  type        = string

  validation {
    condition     = length(trimspace(var.cloudflare_zone_id)) >= 20
    error_message = "cloudflare_zone_id must be a real Cloudflare zone identifier."
  }
}

variable "katcha_hostname" {
  description = "Production Katcha hostname without scheme, path, port, or trailing slash."
  type        = string

  validation {
    condition = (
      length(trimspace(var.katcha_hostname)) >= 3
      && strcontains(var.katcha_hostname, ".")
      && !strcontains(var.katcha_hostname, "/")
      && !strcontains(var.katcha_hostname, ":")
      && lower(var.katcha_hostname) == var.katcha_hostname
    )
    error_message = "katcha_hostname must be a lowercase hostname such as katcha.example.com."
  }
}

variable "operator_emails" {
  description = "Exact operator email addresses allowed through Cloudflare Access."
  type        = set(string)

  validation {
    condition = (
      length(var.operator_emails) >= 1
      && alltrue([
        for email in var.operator_emails :
        can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", email))
      ])
    )
    error_message = "operator_emails must contain at least one valid email address."
  }
}

variable "operator_session_duration" {
  description = "Cloudflare Access session duration for the Katcha operator surface."
  type        = string
  default     = "12h"

  validation {
    condition     = can(regex("^[1-9][0-9]*(m|h)$", var.operator_session_duration))
    error_message = "operator_session_duration must be a duration such as 30m or 12h."
  }
}
