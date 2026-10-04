from __future__ import annotations

import httpx
import pytest

from katcha.ops import r2_bucket_lock


def _config() -> r2_bucket_lock.BucketLockConfig:
    return r2_bucket_lock.BucketLockConfig(
        account_id="abc123",
        api_token="admin-token",
        bucket="katcha-backup-prod",
        prefix="postgres/",
        retention_days=30,
        expiration_days=60,
    )


def test_required_lock_rule_covers_backup_prefix_for_minimum_retention() -> None:
    config = _config()
    r2_bucket_lock.validate_required_rule(
        [
            {
                "id": r2_bucket_lock.RULE_ID,
                "enabled": True,
                "prefix": "postgres/",
                "condition": {
                    "type": "Age",
                    "maxAgeSeconds": 31 * 86400,
                },
            }
        ],
        config,
    )


def test_required_lock_rule_rejects_shorter_retention() -> None:
    config = _config()
    with pytest.raises(r2_bucket_lock.BucketLockError, match="shorter"):
        r2_bucket_lock.validate_required_rule(
            [
                {
                    "id": r2_bucket_lock.RULE_ID,
                    "enabled": True,
                    "prefix": "postgres/",
                    "condition": {
                        "type": "Age",
                        "maxAgeSeconds": 7 * 86400,
                    },
                }
            ],
            config,
        )


def test_apply_preserves_unrelated_bucket_lock_rules(monkeypatch) -> None:
    config = _config()
    existing = {
        "id": "other-rule",
        "enabled": True,
        "prefix": "legal/",
        "condition": {"type": "Indefinite"},
    }
    applied: list[dict[str, object]] = []
    get_count = 0

    def fake_get(url, *, headers, timeout):
        nonlocal get_count
        del url, headers, timeout
        get_count += 1
        rules = [existing] if get_count == 1 else [
            existing,
            config.desired_rule,
        ]
        return httpx.Response(
            200,
            json={"success": True, "result": {"rules": rules}},
            request=httpx.Request("GET", config.api_url),
        )

    def fake_put(url, *, headers, json, timeout):
        del url, headers, timeout
        applied.extend(json["rules"])
        return httpx.Response(
            200,
            json={"success": True, "result": {}},
            request=httpx.Request("PUT", config.api_url),
        )

    monkeypatch.setattr(r2_bucket_lock.httpx, "get", fake_get)
    monkeypatch.setattr(r2_bucket_lock.httpx, "put", fake_put)

    r2_bucket_lock.apply_required_rule(config)

    assert existing in applied
    assert config.desired_rule in applied



def test_required_lifecycle_expires_only_after_retention_window() -> None:
    config = _config()
    r2_bucket_lock.validate_required_lifecycle(
        [
            {
                "id": r2_bucket_lock.LIFECYCLE_RULE_ID,
                "enabled": True,
                "conditions": {"prefix": "postgres/"},
                "deleteObjectsTransition": {
                    "condition": {
                        "type": "Age",
                        "maxAge": 60 * 86400,
                    }
                },
            }
        ],
        config,
    )


def test_required_lifecycle_rejects_early_expiration() -> None:
    config = _config()
    with pytest.raises(r2_bucket_lock.BucketLockError, match="too early"):
        r2_bucket_lock.validate_required_lifecycle(
            [
                {
                    "id": r2_bucket_lock.LIFECYCLE_RULE_ID,
                    "enabled": True,
                    "conditions": {"prefix": "postgres/"},
                    "deleteObjectsTransition": {
                        "condition": {
                            "type": "Age",
                            "maxAge": 20 * 86400,
                        }
                    },
                }
            ],
            config,
        )
