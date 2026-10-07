from __future__ import annotations

from katcha.ops import cloudflare_edge


def _config() -> cloudflare_edge.CloudflareEdgeConfig:
    return cloudflare_edge.CloudflareEdgeConfig(
        zone_id="zone",
        api_token="token",
        hostname="app.katcha.stream",
        public_requests_per_10s=30,
    )


def test_public_health_disables_only_browser_integrity_check() -> None:
    rules = cloudflare_edge.desired_rules(_config())
    config_rules = rules["http_config_settings"]

    assert len(config_rules) == 1
    rule = config_rules[0]
    assert rule["ref"] == "katcha_public_health_disable_bic"
    assert rule["action"] == "set_config"
    assert rule["action_parameters"] == {"bic": False}
    assert 'http.host eq "app.katcha.stream"' in rule["expression"]
    assert '/v1/health/live' in rule["expression"]
    assert '/v1/health/ready' in rule["expression"]
    assert "/v1/integrations/youtube/oauth/callback" not in rule["expression"]


def test_edge_drift_comparison_includes_configuration_parameters() -> None:
    rule = cloudflare_edge.desired_rules(_config())["http_config_settings"][0]
    drifted = {
        **rule,
        "action_parameters": {"bic": True},
    }

    assert cloudflare_edge._comparable(rule) != cloudflare_edge._comparable(drifted)
