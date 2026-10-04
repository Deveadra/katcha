from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

import httpx

RULE_ID = "katcha-postgres-backups"
LIFECYCLE_RULE_ID = "katcha-postgres-backup-expiry"


class BucketLockError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BucketLockConfig:
    account_id: str
    api_token: str
    bucket: str
    prefix: str
    retention_days: int
    expiration_days: int
    jurisdiction: str | None = None

    @classmethod
    def from_env(cls) -> BucketLockConfig:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise BucketLockError(f"missing required setting: {name}")
            return value

        days = int(os.environ.get("KATCHA_BACKUP_RETENTION_DAYS", "30"))
        if days < 7 or days > 3650:
            raise BucketLockError("backup retention must be between 7 and 3650 days")
        expiration_days = int(
            os.environ.get("KATCHA_BACKUP_EXPIRATION_DAYS", "60")
        )
        if expiration_days <= days or expiration_days > 3650:
            raise BucketLockError(
                "backup expiration must be greater than retention and at most 3650 days"
            )
        prefix = os.environ.get("KATCHA_BACKUP_R2_PREFIX", "postgres").strip().strip("/")
        if not prefix:
            raise BucketLockError("KATCHA_BACKUP_R2_PREFIX cannot be empty")
        jurisdiction = os.environ.get("KATCHA_BACKUP_R2_JURISDICTION", "").strip()
        return cls(
            account_id=required("CLOUDFLARE_ACCOUNT_ID"),
            api_token=required("CLOUDFLARE_R2_ADMIN_TOKEN"),
            bucket=required("KATCHA_BACKUP_R2_BUCKET"),
            prefix=f"{prefix}/",
            retention_days=days,
            expiration_days=expiration_days,
            jurisdiction=jurisdiction or None,
        )

    @property
    def api_url(self) -> str:
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/r2/buckets/{self.bucket}/lock"
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
            "prefix": self.prefix,
            "condition": {
                "type": "Age",
                "maxAgeSeconds": self.retention_days * 86400,
            },
        }

    @property
    def desired_lifecycle_rule(self) -> dict[str, object]:
        return {
            "id": LIFECYCLE_RULE_ID,
            "enabled": True,
            "conditions": {"prefix": self.prefix},
            "deleteObjectsTransition": {
                "condition": {
                    "type": "Age",
                    "maxAge": self.expiration_days * 86400,
                }
            },
        }


def _headers(config: BucketLockConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_token}",
        "Content-Type": "application/json",
    }
    if config.jurisdiction:
        headers["cf-r2-jurisdiction"] = config.jurisdiction
    return headers


def get_rules(config: BucketLockConfig) -> list[dict[str, Any]]:
    response = httpx.get(
        config.api_url,
        headers=_headers(config),
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise BucketLockError(
            f"Cloudflare lock lookup failed with HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BucketLockError("Cloudflare lock lookup returned an invalid response")
    result = payload.get("result") or {}
    rules = result.get("rules") or []
    if not isinstance(rules, list):
        raise BucketLockError("Cloudflare lock lookup returned invalid rules")
    return [dict(rule) for rule in rules if isinstance(rule, dict)]


def validate_required_rule(
    rules: list[dict[str, Any]],
    config: BucketLockConfig,
) -> None:
    matches = [rule for rule in rules if rule.get("id") == RULE_ID]
    if len(matches) != 1:
        raise BucketLockError(
            f"expected exactly one enabled {RULE_ID} bucket-lock rule"
        )
    rule = matches[0]
    condition = rule.get("condition") or {}
    max_age = int(condition.get("maxAgeSeconds") or 0)
    if rule.get("enabled") is not True:
        raise BucketLockError(f"{RULE_ID} is not enabled")
    if rule.get("prefix") != config.prefix:
        raise BucketLockError(
            f"{RULE_ID} protects prefix {rule.get('prefix')!r}, "
            f"expected {config.prefix!r}"
        )
    if condition.get("type") != "Age":
        raise BucketLockError(f"{RULE_ID} must use an age retention condition")
    if max_age < config.retention_days * 86400:
        raise BucketLockError(
            f"{RULE_ID} retention is shorter than {config.retention_days} days"
        )


def get_lifecycle_rules(config: BucketLockConfig) -> list[dict[str, Any]]:
    response = httpx.get(
        config.lifecycle_url,
        headers=_headers(config),
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise BucketLockError(
            "Cloudflare lifecycle lookup failed with HTTP "
            f"{response.status_code}: {response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BucketLockError("Cloudflare lifecycle lookup returned an invalid response")
    result = payload.get("result") or {}
    rules = result.get("rules") or []
    if not isinstance(rules, list):
        raise BucketLockError("Cloudflare lifecycle lookup returned invalid rules")
    return [dict(rule) for rule in rules if isinstance(rule, dict)]


def validate_required_lifecycle(
    rules: list[dict[str, Any]],
    config: BucketLockConfig,
) -> None:
    matches = [rule for rule in rules if rule.get("id") == LIFECYCLE_RULE_ID]
    if len(matches) != 1:
        raise BucketLockError(
            f"expected exactly one enabled {LIFECYCLE_RULE_ID} lifecycle rule"
        )
    rule = matches[0]
    transition = rule.get("deleteObjectsTransition") or {}
    condition = transition.get("condition") or {}
    max_age = int(condition.get("maxAge") or 0)
    if rule.get("enabled") is not True:
        raise BucketLockError(f"{LIFECYCLE_RULE_ID} is not enabled")
    if (rule.get("conditions") or {}).get("prefix") != config.prefix:
        raise BucketLockError(
            f"{LIFECYCLE_RULE_ID} does not target {config.prefix!r}"
        )
    if condition.get("type") != "Age":
        raise BucketLockError(
            f"{LIFECYCLE_RULE_ID} must use an age deletion condition"
        )
    if max_age < config.expiration_days * 86400:
        raise BucketLockError(
            f"{LIFECYCLE_RULE_ID} expires backups too early"
        )


def apply_required_rule(config: BucketLockConfig) -> None:
    current = get_rules(config)
    desired = config.desired_rule
    merged = [rule for rule in current if rule.get("id") != RULE_ID]
    merged.append(desired)
    response = httpx.put(
        config.api_url,
        headers=_headers(config),
        json={"rules": merged},
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise BucketLockError(
            f"Cloudflare lock update failed with HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BucketLockError("Cloudflare lock update returned an invalid response")
    validate_required_rule(get_rules(config), config)


def apply_required_lifecycle(config: BucketLockConfig) -> None:
    current = get_lifecycle_rules(config)
    desired = config.desired_lifecycle_rule
    merged = [rule for rule in current if rule.get("id") != LIFECYCLE_RULE_ID]
    merged.append(desired)
    response = httpx.put(
        config.lifecycle_url,
        headers=_headers(config),
        json={"rules": merged},
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise BucketLockError(
            "Cloudflare lifecycle update failed with HTTP "
            f"{response.status_code}: {response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BucketLockError("Cloudflare lifecycle update returned an invalid response")
    validate_required_lifecycle(get_lifecycle_rules(config), config)


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
        config = BucketLockConfig.from_env()
        if args.action == "apply":
            apply_required_rule(config)
            apply_required_lifecycle(config)
        else:
            validate_required_rule(get_rules(config), config)
            validate_required_lifecycle(get_lifecycle_rules(config), config)
    except (BucketLockError, ValueError) as exc:
        print(f"BUCKET_LOCK_ERROR: {exc}")
        return 2

    print(
        f"R2 backup lock verified: bucket={config.bucket} "
        f"prefix={config.prefix} retention_days>={config.retention_days} "
        f"expiration_days={config.expiration_days}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
