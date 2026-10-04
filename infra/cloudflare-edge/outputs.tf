output "operator_access_application_id" {
  description = "Cloudflare Access application protecting the Katcha operator surface."
  value       = cloudflare_zero_trust_access_application.operator.id
}

output "operator_access_aud" {
  description = "Access audience tag for optional origin-side JWT validation."
  value       = cloudflare_zero_trust_access_application.operator.aud
}

output "api_bypass_application_id" {
  description = "More-specific Access application that leaves /v1 to Katcha bearer auth."
  value       = cloudflare_zero_trust_access_application.api_bypass.id
}

output "oauth_callback_application_id" {
  description = "More-specific public Access application for ChatGPT OAuth callback."
  value       = cloudflare_zero_trust_access_application.chatgpt_oauth_callback.id
}
