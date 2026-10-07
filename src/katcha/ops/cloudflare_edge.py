from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any, Literal

import httpx


class CloudflareEdgeError(RuntimeError):
    pass


Phase = Literal[\n    "http_config_settings",\n    "http_request_firewall_custom",\n    "http_ratelimit",\n]

_CONFIG_PHASE: Phase = "http_config_settings"
_CUSTOM_PHASE: Phase = "http_request_firewall_custom"
_RATE_PHASE: Phase = "http_ratelimit"

_METHOD_RULE_REF = "katcha_public_method_guard"
_PROBE_RULE_REF = "katcha_sensitive_probe_block"
_RATE_RULE_REF = "katcha_public_endpoint_rate_limit"
_HEALTH_BIC_RULE_REF = "katcha_public_health_disable_bic"


@dataclass(frozen=True, slots=True)
class CloudflareEdgeConfig:
    zone_id: str
    api_token: str
    hostname: str
    public_requests_per_10s: int

    @classmethod
    def from_env(cls) -> CloudflareEdgeConfig:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise CloudflareEdgeError(f"missing required setting: {name}")
            return value

        hostname = required("KATCHA_PUBLIC_HOSTNAME").casefold()
        if (
            "." not in hostname
            or "/" in hostname
            or ":" in hostname
            or hostname.startswith(".")
            or hostname.endswith(".")
        ):
            raise CloudflareEdgeError(
                "KATCHA_PUBLIC_HOSTNAME must be a hostname without scheme, path, or port"
            )
        requests = int(
            os.environ.get(
                "KATCHA_PUBLIC_RATE_LIMIT_REQUESTS_PER_10S",
                "30",
            )
        )
        if requests < 10 or requests > 1000:
            raise CloudflareEdgeError(
                "public rate limit must be between 10 and 1000 requests per 10 seconds"
            )
        return cls(
            zone_id=required("CLOUDFLARE_ZONE_ID"),
            api_token=required("CLOUDFLARE_API_TOKEN"),
            hostname=hostname,
            public_requests_per_10s=requests,
        )


class CloudflareRulesetsClient:
    def __init__(
        self,
        config: CloudflareEdgeConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.client = client or httpx.Client(timeout=20.0)
        self.base = (
            "https://api.cloudflare.com/client/v4/zones/"
            f"{config.zone_id}/rulesets"
        )
        self.headers = {
            "Authorization": f"Bearer {config.api_token}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        response = self.client.request(
            method,
            url,
            headers=self.headers,
            json=payload,
        )
        if allow_missing and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise CloudflareEdgeError(
                f"Cloudflare {method} failed with HTTP {response.status_code}: "
                f"{response.text[:400]}"
            )
        data = response.json()
        if not isinstance(data, dict) or data.get("success") is not True:
            raise CloudflareEdgeError(
                f"Cloudflare {method} returned an invalid response"
            )
        result = data.get("result")
        if not isinstance(result, dict):
            raise CloudflareEdgeError(
                f"Cloudflare {method} response did not contain a ruleset"
            )
        return result

    def entrypoint(self, phase: Phase) -> dict[str, Any] | None:
        return self._request(
            "GET",
            f"{self.base}/phases/{phase}/entrypoint",
            allow_missing=True,
        )

    def create_entrypoint(self, phase: Phase) -> dict[str, Any]:
        result = self._request(
            "POST",
            self.base,
            payload={
                "name": f"Katcha {phase}",
                "description": (
                    "Katcha-managed rules. Unrelated existing rules are preserved."
                ),
                "kind": "zone",
                "phase": phase,
                "rules": [],
            },
        )
        assert result is not None
        return result

    def ensure_entrypoint(self, phase: Phase) -> dict[str, Any]:
        return self.entrypoint(phase) or self.create_entrypoint(phase)

    def create_rule(
        self,
        ruleset_id: str,
        rule: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"{self.base}/{ruleset_id}/rules",
            payload=rule,
        )
        assert result is not None
        return result

    def update_rule(
        self,
        ruleset_id: str,
        rule_id: str,
        rule: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._request(
            "PATCH",
            f"{self.base}/{ruleset_id}/rules/{rule_id}",
            payload=rule,
        )
        assert result is not None
        return result



def _config_rules(config: CloudflareEdgeConfig) -> list[dict[str, Any]]:
    host = config.hostname
    public_health = (
        '(http.request.uri.path eq "/v1/health/live" '
        'or http.request.uri.path eq "/v1/health/ready")'
    )
    return [
        {
            "ref": _HEALTH_BIC_RULE_REF,
            "description": (
                "Katcha: disable Browser Integrity Check only for public health probes"
            ),
            "expression": f'(http.host eq "{host}" and {public_health})',
            "action": "set_config",
            "action_parameters": {"bic": False},
            "enabled": True,
        }
    ]


def _custom_rules(config: CloudflareEdgeConfig) -> list[dict[str, Any]]:
    host = config.hostname
    public_health = (
        '(http.request.uri.path eq "/v1/health/live" '
        'or http.request.uri.path eq "/v1/health/ready")'
    )
    oauth_callbacks = (
        '(http.request.uri.path eq "/v1/integrations/youtube/oauth/callback" '
        'or http.request.uri.path eq "/auth/callback")'
    )
    method_guard = (
        f'(http.host eq "{host}" and ('
        f'({public_health} and '
        '(http.request.method ne "GET" and http.request.method ne "HEAD")) '
        f'or ({oauth_callbacks} and http.request.method ne "GET")))'
    )
    sensitive_probes = (
        f'(http.host eq "{host}" and ('
        'http.request.uri.path eq "/.env" '
        'or starts_with(http.request.uri.path, "/.git") '
        'or starts_with(http.request.uri.path, "/.aws") '
        'or http.request.uri.path eq "/server-status" '
        'or starts_with(http.request.uri.path, "/phpmyadmin")))'
    )
    return [
        {
            "ref": _METHOD_RULE_REF,
            "description": (
                "Katcha: block invalid methods on unauthenticated health/OAuth endpoints"
            ),
            "expression": method_guard,
            "action": "block",
            "enabled": True,
        },
        {
            "ref": _PROBE_RULE_REF,
            "description": "Katcha: block common secret and admin-path probes",
            "expression": sensitive_probes,
            "action": "block",
            "enabled": True,
        },
    ]


def _rate_rule(config: CloudflareEdgeConfig) -> dict[str, Any]:
    # Free-plan rate limiting can match Path but not Host. These are deliberately
    # Katcha-specific paths to avoid consuming the one free rule on broad traffic.
    expression = (
        '(http.request.uri.path eq "/v1/health/live" '
        'or http.request.uri.path eq "/v1/health/ready" '
        'or http.request.uri.path eq "/v1/integrations/youtube/oauth/callback")'
    )
    return {
        "ref": _RATE_RULE_REF,
        "description": "Katcha: bound abuse of public health and YouTube OAuth callback",
        "expression": expression,
        "action": "block",
        "enabled": True,
        "ratelimit": {
            "characteristics": ["cf.colo.id", "ip.src"],
            "period": 10,
            "requests_per_period": config.public_requests_per_10s,
            "mitigation_timeout": 10,
            "requests_to_origin": True,
        },
    }


def desired_rules(
    config: CloudflareEdgeConfig,
) -> dict[Phase, list[dict[str, Any]]]:
    return {
        _CONFIG_PHASE: _config_rules(config),
        _CUSTOM_PHASE: _custom_rules(config),
        _RATE_PHASE: [_rate_rule(config)],
    }


def _rules_by_ref(ruleset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = ruleset.get("rules") or []
    if not isinstance(rows, list):
        raise CloudflareEdgeError("Cloudflare ruleset returned an invalid rules list")
    return {
        str(row.get("ref")): row
        for row in rows
        if isinstance(row, dict) and row.get("ref")
    }


def _comparable(rule: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ref",
        "description",
        "expression",
        "action",
        "enabled",
        "ratelimit",
        "action_parameters",
    )
    return {
        key: rule.get(key)
        for key in keys
        if key in rule
    }


def apply_rules(
    config: CloudflareEdgeConfig,
    api: CloudflareRulesetsClient,
) -> list[str]:
    changes: list[str] = []
    for phase, wanted in desired_rules(config).items():
        ruleset = api.ensure_entrypoint(phase)
        ruleset_id = str(ruleset.get("id") or "")
        if not ruleset_id:
            raise CloudflareEdgeError(
                f"Cloudflare {phase} entrypoint has no ruleset ID"
            )
        existing = _rules_by_ref(ruleset)
        for rule in wanted:
            current = existing.get(str(rule["ref"]))
            if current is None:
                api.create_rule(ruleset_id, rule)
                changes.append(f"created:{phase}:{rule['ref']}")
                continue
            rule_id = str(current.get("id") or "")
            if not rule_id:
                raise CloudflareEdgeError(
                    f"existing Katcha rule {rule['ref']} has no rule ID"
                )
            if _comparable(current) != _comparable(rule):
                api.update_rule(ruleset_id, rule_id, rule)
                changes.append(f"updated:{phase}:{rule['ref']}")
    return changes


def check_rules(
    config: CloudflareEdgeConfig,
    api: CloudflareRulesetsClient,
) -> None:
    for phase, wanted in desired_rules(config).items():
        ruleset = api.entrypoint(phase)
        if ruleset is None:
            raise CloudflareEdgeError(
                f"Cloudflare {phase} entrypoint does not exist"
            )
        existing = _rules_by_ref(ruleset)
        for rule in wanted:
            current = existing.get(str(rule["ref"]))
            if current is None:
                raise CloudflareEdgeError(
                    f"missing Cloudflare edge rule: {rule['ref']}"
                )
            if _comparable(current) != _comparable(rule):
                raise CloudflareEdgeError(
                    f"Cloudflare edge rule drift detected: {rule['ref']}"
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["check", "apply"],
        nargs="?",
        default="check",
    )
    args = parser.parse_args()
    try:
        config = CloudflareEdgeConfig.from_env()
        with httpx.Client(timeout=20.0) as client:
            api = CloudflareRulesetsClient(config, client=client)
            if args.action == "apply":
                changes = apply_rules(config, api)
                check_rules(config, api)
                print(
                    "Katcha Cloudflare edge rules applied: "
                    + (", ".join(changes) if changes else "no changes")
                )
            else:
                check_rules(config, api)
                print("Katcha Cloudflare edge rules match the required policy.")
    except (CloudflareEdgeError, ValueError) as exc:
        print(f"CLOUDFLARE_EDGE_ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
