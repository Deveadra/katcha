from __future__ import annotations

import pytest

from katcha.ops import break_glass_lifecycle


def test_desired_lifecycle_rule_is_bounded_to_handoff_prefix() -> None:
    config = break_glass_lifecycle.BreakGlassLifecycleConfig(
        account_id="account",
        api_token="token",
        bucket="handoff-bucket",
        prefix="bootstrap-handoff/",
        expiration_seconds=86400,
    )

    assert config.desired_rule == {
        "id": "katcha-break-glass-handoff-expiry",
        "enabled": True,
        "conditions": {"prefix": "bootstrap-handoff/"},
        "deleteObjectsTransition": {
            "condition": {
                "type": "Age",
                "maxAge": 86400,
            }
        },
    }


def test_validate_rule_rejects_expiry_longer_than_policy() -> None:
    config = break_glass_lifecycle.BreakGlassLifecycleConfig(
        account_id="account",
        api_token="token",
        bucket="handoff-bucket",
        prefix="bootstrap-handoff/",
        expiration_seconds=86400,
    )
    rules = [
        {
            "id": "katcha-break-glass-handoff-expiry",
            "enabled": True,
            "conditions": {"prefix": "bootstrap-handoff/"},
            "deleteObjectsTransition": {
                "condition": {
                    "type": "Age",
                    "maxAge": 172800,
                }
            },
        }
    ]

    with pytest.raises(
        break_glass_lifecycle.BreakGlassLifecycleError,
        match="must expire handoffs within",
    ):
        break_glass_lifecycle.validate_rule(rules, config)


def test_validate_rule_preserves_unrelated_lifecycle_rules() -> None:
    config = break_glass_lifecycle.BreakGlassLifecycleConfig(
        account_id="account",
        api_token="token",
        bucket="handoff-bucket",
        prefix="bootstrap-handoff/",
        expiration_seconds=86400,
    )
    rules = [
        {
            "id": "other-rule",
            "enabled": True,
            "conditions": {"prefix": "other/"},
            "deleteObjectsTransition": {
                "condition": {
                    "type": "Age",
                    "maxAge": 604800,
                }
            },
        },
        config.desired_rule,
    ]

    break_glass_lifecycle.validate_rule(rules, config)
