from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

import httpx

RULE_ID = "katcha-break-glass-handoff-expiry"


class BreakGlassLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BreakGlassLifecycleConfig:
    account_id: str
    api_token: str
    bucket: str
    prefix: str
    expiration_seconds: int
    jurisdiction: str | None = None

    @classmethod
    def from_env(cls) -> BreakGlassLifecycleConfig:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise BreakGlassLifecycleError(
                    f"missing required setting: {name}"
                )
            return value

        prefix = os.environ.get(
            "KATCHA_BREAK_GLASS_R2_PREFIX",
            "bootstrap-handoff",
        ).strip().strip("/")
        if not prefix:
            raise BreakGlassLifecycleError(
                "KATCHA_BREAK_GLASS_R2_PREFIX cannot be empty"
            )
        expiration_seconds = int(
            os.environ.get(
                "KATCHA_BREAK_GLASS_R2_EXPIRATION_SECONDS",
                "86400",
            )
        )
        if expiration_seconds < 3600 or expiration_seconds > 86400:
            raise BreakGlassLifecycleError(
                "break-glass handoff lifecycle must be between 3600 and "
                "86400 seconds"
            )
        jurisdiction = os.environ.get(
            "KATCHA_BREAK_GLASS_R2_JURISDICTION",
            "",
        ).strip()
        return cls(
            account_id=required("CLOUDFLARE_ACCOUNT_ID"),
            api_token=required("CLOUDFLARE_R2_ADMIN_TOKEN"),
            bucket=required("KATCHA_BREAK_GLASS_R2_BUCKET"),
            prefix=f"{prefix}/",
            expiration_seconds=expiration_seconds,
            jurisdiction=jurisdiction or None,
        )

    @property
    def lifecycle_url(self) -> str:
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/r2/buckets/{self.bucket}/lifecycle"
        )

    @property
    def desired_rule(self) -> dict[str, object]:
        return {
            "id": RULE_ID,
            "enabled": True,
            "conditions": {"prefix": self.prefix},
            "deleteObjectsTransition": {
                "condition": {
                    "type": "Age",
                    "maxAge": self.expiration_seconds,
                }
            },
        }


def _headers(config: BreakGlassLifecycleConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_token}",
        "Content-Type": "application/json",
    }
    if config.jurisdiction:
        headers["cf-r2-jurisdiction"] = config.jurisdiction
    return headers


def get_rules(config: BreakGlassLifecycleConfig) -> list[dict[str, Any]]:
    response = httpx.get(
        config.lifecycle_url,
        headers=_headers(config),
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise BreakGlassLifecycleError(
            "Cloudflare lifecycle lookup failed with HTTP "
            f"{response.status_code}: {response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BreakGlassLifecycleError(
            "Cloudflare lifecycle lookup returned an invalid response"
        )
    result = payload.get("result") or {}
    rules = result.get("rules") or []
    if not isinstance(rules, list):
        raise BreakGlassLifecycleError(
            "Cloudflare lifecycle lookup returned invalid rules"
        )
    return [dict(rule) for rule in rules if isinstance(rule, dict)]


def validate_rule(
    rules: list[dict[str, Any]],
    config: BreakGlassLifecycleConfig,
) -> None:
    matches = [rule for rule in rules if rule.get("id") == RULE_ID]
    if len(matches) != 1:
        raise BreakGlassLifecycleError(
            f"expected exactly one enabled {RULE_ID} lifecycle rule"
        )
    rule = matches[0]
    transition = rule.get("deleteObjectsTransition") or {}
    condition = transition.get("condition") or {}
    max_age = int(condition.get("maxAge") or 0)
    if rule.get("enabled") is not True:
        raise BreakGlassLifecycleError(f"{RULE_ID} is not enabled")
    if (rule.get("conditions") or {}).get("prefix") != config.prefix:
        raise BreakGlassLifecycleError(
            f"{RULE_ID} does not target {config.prefix!r}"
        )
    if condition.get("type") != "Age":
        raise BreakGlassLifecycleError(
            f"{RULE_ID} must use an age deletion condition"
        )
    if max_age <= 0 or max_age > config.expiration_seconds:
        raise BreakGlassLifecycleError(
            f"{RULE_ID} must expire handoffs within "
            f"{config.expiration_seconds} seconds"
        )


def apply_rule(config: BreakGlassLifecycleConfig) -> None:
    current = get_rules(config)
    merged = [
        rule
        for rule in current
        if rule.get("id") != RULE_ID
    ]
    merged.append(config.desired_rule)
    response = httpx.put(
        config.lifecycle_url,
        headers=_headers(config),
        json={"rules": merged},
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise BreakGlassLifecycleError(
            "Cloudflare lifecycle update failed with HTTP "
            f"{response.status_code}: {response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BreakGlassLifecycleError(
            "Cloudflare lifecycle update returned an invalid response"
        )
    validate_rule(get_rules(config), config)


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
        config = BreakGlassLifecycleConfig.from_env()
        if args.action == "apply":
            apply_rule(config)
        else:
            validate_rule(get_rules(config), config)
    except (BreakGlassLifecycleError, ValueError) as exc:
        print(f"BREAK_GLASS_LIFECYCLE_ERROR: {exc}")
        return 2
    print(
        "Break-glass R2 lifecycle verified: "
        f"bucket={config.bucket} prefix={config.prefix} "
        f"expiration_seconds<={config.expiration_seconds}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
