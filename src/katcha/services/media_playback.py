"""Short-lived, cookie-safe grants for native clip media playback."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

from katcha.config import Settings, get_settings

MEDIA_PLAYBACK_COOKIE = "katcha_media_playback"
MEDIA_PLAYBACK_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class MediaPlaybackGrant:
    clip_id: uuid.UUID
    channel_profile_id: uuid.UUID
    expires_at: int


def _signing_key(settings: Settings) -> bytes:
    secrets: list[str] = []
    if settings.control_api_token is not None:
        secrets.append(settings.control_api_token.get_secret_value())
    for principal in settings.control_principals:
        secrets.extend(
            credential.token.get_secret_value()
            for credential in principal.resolved_credentials()
        )
    if not secrets and settings.s3_secret_key:
        secrets.append(settings.s3_secret_key)
    if not secrets:
        raise ValueError("media playback signing material is not configured")
    fingerprints = sorted(hashlib.sha256(value.encode()).hexdigest() for value in secrets)
    return hashlib.sha256(
        ("katcha-media-playback-v1\0" + "\0".join(fingerprints)).encode()
    ).digest()


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_media_playback_grant(
    clip_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    now: int | None = None,
    ttl_seconds: int = MEDIA_PLAYBACK_TTL_SECONDS,
) -> tuple[str, MediaPlaybackGrant]:
    if ttl_seconds < 1 or ttl_seconds > MEDIA_PLAYBACK_TTL_SECONDS:
        raise ValueError("media playback grant lifetime is invalid")
    current = int(time.time()) if now is None else int(now)
    grant = MediaPlaybackGrant(
        clip_id=clip_id,
        channel_profile_id=channel_profile_id,
        expires_at=current + ttl_seconds,
    )
    payload = json.dumps(
        {
            "clip_id": str(grant.clip_id),
            "channel_profile_id": str(grant.channel_profile_id),
            "expires_at": grant.expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(
        _signing_key(settings or get_settings()),
        payload,
        hashlib.sha256,
    ).digest()
    return f"{_encode(payload)}.{_encode(signature)}", grant


def validate_media_playback_grant(
    token: str,
    clip_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> MediaPlaybackGrant | None:
    try:
        payload_value, signature_value = token.split(".", 1)
        payload = _decode(payload_value)
        signature = _decode(signature_value)
        expected = hmac.new(
            _signing_key(settings or get_settings()),
            payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        raw = json.loads(payload)
        grant = MediaPlaybackGrant(
            clip_id=uuid.UUID(str(raw["clip_id"])),
            channel_profile_id=uuid.UUID(str(raw["channel_profile_id"])),
            expires_at=int(raw["expires_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    current = int(time.time()) if now is None else int(now)
    if grant.expires_at <= current:
        return None
    if grant.clip_id != clip_id or grant.channel_profile_id != channel_profile_id:
        return None
    return grant
