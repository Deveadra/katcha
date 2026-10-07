from __future__ import annotations

import json
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from katcha.config import Settings, get_settings
from katcha.security.secrets import SecretConfigurationError

_EPHEMERAL_DEVELOPMENT_KEY = Fernet.generate_key()
SOURCE_MEDIA_COOKIE = "katcha_editorial_source_media"
SOURCE_MEDIA_TICKET_TTL_SECONDS = 15 * 60


def _cipher(settings: Settings | None = None) -> Fernet:
    settings = settings or get_settings()
    if settings.credential_encryption_key:
        try:
            return Fernet(settings.credential_encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SecretConfigurationError(
                "KATCHA_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key"
            ) from exc
    if settings.env != "production":
        return Fernet(_EPHEMERAL_DEVELOPMENT_KEY)
    raise SecretConfigurationError(
        "KATCHA_CREDENTIAL_ENCRYPTION_KEY is required for private media tickets"
    )


def issue_media_ticket(
    scope: dict[str, str],
    *,
    settings: Settings | None = None,
    ttl_seconds: int = SOURCE_MEDIA_TICKET_TTL_SECONDS,
) -> str:
    if ttl_seconds < 30 or ttl_seconds > 3600:
        raise ValueError("media ticket TTL must be between 30 and 3600 seconds")
    payload: dict[str, Any] = {
        "version": 1,
        "expires_at": int(time.time()) + ttl_seconds,
        "scope": scope,
    }
    return _cipher(settings).encrypt(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def verify_media_ticket(
    token: str,
    expected_scope: dict[str, str],
    *,
    settings: Settings | None = None,
) -> None:
    if not token:
        raise ValueError("private media ticket is missing")
    try:
        raw = _cipher(settings).decrypt(token.encode("ascii"))
        payload = json.loads(raw)
    except (InvalidToken, UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise ValueError("private media ticket is invalid or expired") from exc
    if payload.get("version") != 1:
        raise ValueError("private media ticket version is unsupported")
    if int(payload.get("expires_at") or 0) < int(time.time()):
        raise ValueError("private media ticket is expired")
    if payload.get("scope") != expected_scope:
        raise ValueError("private media ticket does not match this media source")
