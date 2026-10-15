"""Encrypted hosted AI configuration, shared by API and workflow workers."""

from __future__ import annotations

from sqlalchemy import select

from katcha.config import Settings
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.provider_setting_models import ApplicationAISetting
from katcha.runtime_fence import assert_mutation_authority
from katcha.security.secrets import decrypt_secret, encrypt_secret

FIELDS = {
    "KATCHA_AI_EXECUTION_MODE": "ai_execution_mode",
    "KATCHA_CONVERSATION_PROVIDER": "conversation_provider",
    "KATCHA_AGENT_PROVIDER": "agent_provider",
    "KATCHA_ALLOW_PAID_OPENAI_FALLBACK": "allow_paid_openai_fallback",
    "KATCHA_OPENAI_API_KEY": "openai_api_key",
    "KATCHA_GEMINI_API_KEY": "gemini_api_key",
    "KATCHA_AI_ENABLED": "ai_enabled",
}
SECRET_KEYS = {"KATCHA_OPENAI_API_KEY", "KATCHA_GEMINI_API_KEY"}
CHOICES = {
    "KATCHA_AI_EXECUTION_MODE": {"fixture", "live", "auto"},
    "KATCHA_CONVERSATION_PROVIDER": {"gemini", "codex", "auto"},
    "KATCHA_AGENT_PROVIDER": {"gemini", "codex", "auto"},
    "KATCHA_ALLOW_PAID_OPENAI_FALLBACK": {"true", "false"},
    "KATCHA_AI_ENABLED": {"true", "false"},
}


def effective_settings(base: Settings) -> Settings:
    with session_scope() as session:
        rows = session.scalars(select(ApplicationAISetting)).all()
        overrides = {}
        for row in rows:
            if row.key not in FIELDS:
                continue
            value = decrypt_secret(row.value, base) if row.key in SECRET_KEYS else row.value
            if row.key in {"KATCHA_AI_ENABLED", "KATCHA_ALLOW_PAID_OPENAI_FALLBACK"}:
                value = value == "true"
            overrides[FIELDS[row.key]] = value
    return base.model_copy(update=overrides) if overrides else base


def masked_settings(base: Settings) -> dict:
    resolved = effective_settings(base)
    settings = {}
    for key, field in FIELDS.items():
        value = getattr(resolved, field)
        settings[key] = (
            bool(value)
            if key in SECRET_KEYS
            else (str(value).lower() if isinstance(value, bool) else value)
        )
    return {"settings": settings, "scope": "hosted", "applied": True}


def save_settings(changes: dict[str, str], base: Settings, *, actor: str) -> dict:
    if set(changes) - FIELDS.keys():
        raise ValueError("Unknown AI setting")
    clean = {}
    for key, value in changes.items():
        if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n\x00"):
            raise ValueError("AI settings must be single-line text")
        value = value.strip()
        if key in SECRET_KEYS:
            if not value:
                continue  # A blank input retains the existing credential.
            clean[key] = encrypt_secret(value, base)
        else:
            if value not in CHOICES[key]:
                raise ValueError("Invalid AI setting choice")
            clean[key] = value
    if clean.get("KATCHA_AI_EXECUTION_MODE") == "live":
        clean["KATCHA_AI_ENABLED"] = "true"
    assert_mutation_authority("application_ai_settings.save", settings=base)
    with session_scope() as session:
        for key, value in clean.items():
            row = session.get(ApplicationAISetting, key)
            if row is None:
                session.add(ApplicationAISetting(key=key, value=value))
            else:
                row.value = value
        session.add(
            DomainEvent(
                aggregate_type="application_ai_settings",
                aggregate_id="shared",
                event_type="application_ai_settings.updated",
                payload={"keys": sorted(clean), "actor": actor},
            )
        )
    return masked_settings(base)
