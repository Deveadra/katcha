from __future__ import annotations

import time

import pytest
from cryptography.fernet import Fernet

from katcha.config import Settings
from katcha.security.media_tickets import issue_media_ticket, verify_media_ticket
from katcha.security.secrets import SecretConfigurationError


def settings(*, env: str = "test", key: str | None = None) -> Settings:
    return Settings(
        _env_file=None,
        env=env,
        credential_encryption_key=key,
        control_api_token=None,
        control_principals=[],
    )


def test_media_ticket_is_exact_scope_and_short_lived(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    cfg = settings(key=key)
    scope = {
        "kind": "editorial_source_media",
        "channel_profile_id": "channel",
        "project_id": "project",
        "run_id": "run",
        "candidate_id": "candidate",
    }
    now = int(time.time())
    monkeypatch.setattr("katcha.security.media_tickets.time.time", lambda: now)
    token = issue_media_ticket(scope, settings=cfg, ttl_seconds=60)

    verify_media_ticket(token, scope, settings=cfg)

    with pytest.raises(ValueError, match="does not match"):
        verify_media_ticket(
            token,
            {**scope, "candidate_id": "another-candidate"},
            settings=cfg,
        )

    monkeypatch.setattr(
        "katcha.security.media_tickets.time.time",
        lambda: now + 61,
    )
    with pytest.raises(ValueError, match="expired"):
        verify_media_ticket(token, scope, settings=cfg)


def test_production_media_ticket_requires_configured_encryption_key():
    with pytest.raises(SecretConfigurationError, match="required"):
        issue_media_ticket(
            {
                "kind": "editorial_source_media",
                "channel_profile_id": "channel",
                "project_id": "project",
                "run_id": "run",
                "candidate_id": "candidate",
            },
            settings=settings(env="production"),
        )


def test_development_media_ticket_can_use_ephemeral_process_key():
    cfg = settings()
    scope = {
        "kind": "editorial_source_media",
        "channel_profile_id": "channel",
        "project_id": "project",
        "run_id": "run",
        "candidate_id": "candidate",
    }
    token = issue_media_ticket(scope, settings=cfg)
    verify_media_ticket(token, scope, settings=cfg)
