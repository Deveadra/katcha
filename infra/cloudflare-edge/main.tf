locals {
  operator_allow = [
    for email in sort(tolist(var.operator_emails)) : {
      email = {
        email = email
      }
    }
  ]
}

resource "cloudflare_zero_trust_access_application" "operator" {
  zone_id          = var.cloudflare_zone_id
  name             = "Katcha operator surface"
  domain           = var.katcha_hostname
  type             = "self_hosted"
  session_duration = var.operator_session_duration

  app_launcher_visible      = false
  auto_redirect_to_identity = false

  destinations = [{
    type = "public"
    uri  = "${var.katcha_hostname}/*"
  }]

  policies = [{
    name       = "Allow named Katcha operators"
    decision   = "allow"
    precedence = 1
    include    = local.operator_allow
  }]
}

resource "cloudflare_zero_trust_access_application" "api_bypass" {
  zone_id              = var.cloudflare_zone_id
  name                 = "Katcha authenticated API bypass"
  domain               = "${var.katcha_hostname}/v1/*"
  type                 = "self_hosted"
  app_launcher_visible = false

  destinations = [{
    type = "public"
    uri  = "${var.katcha_hostname}/v1/*"
  }]

  policies = [{
    name       = "Bypass Access for Katcha bearer API"
    decision   = "bypass"
    precedence = 1
    include = [{
      everyone = {}
    }]
  }]
}

resource "cloudflare_zero_trust_access_application" "chatgpt_oauth_callback" {
  zone_id              = var.cloudflare_zone_id
  name                 = "Katcha ChatGPT OAuth callback"
  domain               = "${var.katcha_hostname}/auth/callback"
  type                 = "self_hosted"
  app_launcher_visible = false

  destinations = [{
    type = "public"
    uri  = "${var.katcha_hostname}/auth/callback"
  }]

  policies = [{
    name       = "Public OAuth callback"
    decision   = "bypass"
    precedence = 1
    include = [{
      everyone = {}
    }]
  }]
}
