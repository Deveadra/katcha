from __future__ import annotations

import uuid
from typing import Any

import httpx

from katcha.config import Settings, get_settings
from katcha.services.provider_settings import get_channel_provider_setting


class ElevenLabsIntegrationError(RuntimeError):
    pass


def _headers(settings: Settings) -> dict[str, str]:
    if not settings.elevenlabs_api_key:
        raise ElevenLabsIntegrationError("ElevenLabs API key is not configured")
    return {"xi-api-key": settings.elevenlabs_api_key}


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=settings.elevenlabs_base_url.rstrip("/"),
        headers=_headers(settings),
        timeout=httpx.Timeout(settings.elevenlabs_timeout_seconds),
    )


def _channel_config(
    channel_profile_id: uuid.UUID | None,
) -> dict[str, object] | None:
    if channel_profile_id is None:
        return {}
    row = get_channel_provider_setting(channel_profile_id, "elevenlabs")
    if row is None:
        return {}
    if not row.enabled:
        # An explicit channel-level Off setting must override the legacy/global
        # ElevenLabs fallback. Returning None distinguishes "disabled here"
        # from "no channel override exists".
        return None
    return dict(row.config or {})


def resolve_elevenlabs_voice(
    *,
    channel_profile_id: uuid.UUID | None = None,
    settings: Settings | None = None,
    role: str | None = None,
) -> tuple[str | None, str]:
    settings = settings or get_settings()
    config = _channel_config(channel_profile_id)
    if config is None:
        return None, settings.elevenlabs_model_id.strip()

    role_key = {
        "longform_primary": "longform_primary_voice_id",
        "longform_secondary": "longform_secondary_voice_id",
    }.get(role or "")
    voice_id = str(
        (config.get(role_key) if role_key else None)
        or config.get("voice_id")
        or settings.elevenlabs_voice_id
        or ""
    ).strip() or None
    model_id = str(config.get("model_id") or settings.elevenlabs_model_id).strip()
    return voice_id, model_id


def elevenlabs_status(
    settings: Settings | None = None,
    *,
    channel_profile_id: uuid.UUID | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    voice_id, model_id = resolve_elevenlabs_voice(
        channel_profile_id=channel_profile_id,
        settings=settings,
    )
    if not settings.elevenlabs_api_key:
        return {
            "configured": False,
            "connected": False,
            "voice_id": voice_id,
            "voice_name": None,
            "voice_category": None,
            "voice_labels": {},
            "model_id": model_id,
            "output_format": settings.elevenlabs_output_format,
            "subscription": None,
            "detail": "Set KATCHA_ELEVENLABS_API_KEY to connect ElevenLabs.",
        }

    try:
        with _client(settings) as client:
            subscription_response = client.get("/v1/user/subscription")
            subscription_response.raise_for_status()
            voice: dict[str, Any] = {}
            if voice_id:
                voice_response = client.get(f"/v1/voices/{voice_id}")
                voice_response.raise_for_status()
                voice = dict(voice_response.json() or {})
    except httpx.HTTPError as exc:
        raise ElevenLabsIntegrationError(
            f"ElevenLabs connection check failed: {exc}"
        ) from exc

    subscription = dict(subscription_response.json() or {})
    used = int(subscription.get("character_count") or 0)
    limit = int(subscription.get("character_limit") or 0)
    detail = (
        "ElevenLabs API and selected channel voice are reachable."
        if voice_id
        else "ElevenLabs API is connected. Select a voice for this channel."
    )
    return {
        "configured": True,
        "connected": True,
        "voice_id": voice_id,
        "voice_name": voice.get("name"),
        "voice_category": voice.get("category"),
        "voice_labels": dict(voice.get("labels") or {}),
        "model_id": model_id,
        "output_format": settings.elevenlabs_output_format,
        "subscription": {
            "tier": subscription.get("tier"),
            "status": subscription.get("status"),
            "character_count": used,
            "character_limit": limit,
            "remaining_characters": max(0, limit - used),
            "next_reset_unix": subscription.get("next_character_count_reset_unix"),
            "max_credit_limit_extension": subscription.get("max_credit_limit_extension"),
            "current_overage": subscription.get("current_overage"),
        },
        "detail": detail,
    }


def get_elevenlabs_voice(
    voice_id: str,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    voice_id = voice_id.strip()
    if not voice_id:
        raise ValueError("voice_id is required")
    try:
        with _client(settings) as client:
            response = client.get(f"/v1/voices/{voice_id}")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ElevenLabsIntegrationError(
            f"ElevenLabs voice lookup failed: {exc}"
        ) from exc

    voice = dict(response.json() or {})
    return {
        "voice_id": voice.get("voice_id") or voice_id,
        "name": voice.get("name"),
        "category": voice.get("category"),
        "description": voice.get("description"),
        "labels": dict(voice.get("labels") or {}),
        "preview_url": voice.get("preview_url"),
        "is_owner": voice.get("is_owner"),
        "is_legacy": voice.get("is_legacy"),
    }


def search_elevenlabs_voices(
    *,
    search: str | None = None,
    page_size: int = 25,
    next_page_token: str | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    settings = settings or get_settings()
    params: dict[str, Any] = {
        "page_size": max(1, min(100, page_size)),
        "include_total_count": True,
    }
    if search:
        params["search"] = search
    if next_page_token:
        params["next_page_token"] = next_page_token

    try:
        with _client(settings) as client:
            response = client.get("/v2/voices", params=params)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ElevenLabsIntegrationError(
            f"ElevenLabs voice search failed: {exc}"
        ) from exc

    payload = dict(response.json() or {})
    voices = []
    for voice in payload.get("voices") or []:
        voices.append(
            {
                "voice_id": voice.get("voice_id"),
                "name": voice.get("name"),
                "category": voice.get("category"),
                "description": voice.get("description"),
                "labels": dict(voice.get("labels") or {}),
                "preview_url": voice.get("preview_url"),
                "is_owner": voice.get("is_owner"),
                "is_legacy": voice.get("is_legacy"),
            }
        )
    return {
        "voices": voices,
        "has_more": bool(payload.get("has_more")),
        "total_count": payload.get("total_count"),
        "next_page_token": payload.get("next_page_token"),
    }


def list_elevenlabs_models(
    settings: Settings | None = None,
) -> list[dict[str, object]]:
    settings = settings or get_settings()
    try:
        with _client(settings) as client:
            response = client.get("/v1/models")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ElevenLabsIntegrationError(
            f"ElevenLabs model discovery failed: {exc}"
        ) from exc

    models: list[dict[str, object]] = []
    for row in response.json() or []:
        if not row.get("can_do_text_to_speech"):
            continue
        models.append(
            {
                "model_id": row.get("model_id"),
                "name": row.get("name"),
                "description": row.get("description"),
                "maximum_text_length_per_request": row.get(
                    "maximum_text_length_per_request"
                ),
                "token_cost_factor": row.get("token_cost_factor"),
            }
        )
    return models


def generate_elevenlabs_preview(
    text: str,
    *,
    voice_id: str,
    model_id: str,
    stability: float = 0.42,
    similarity_boost: float = 0.75,
    style: float = 0.0,
    speed: float = 1.03,
    settings: Settings | None = None,
) -> tuple[bytes, dict[str, object]]:
    settings = settings or get_settings()
    text = text.strip()
    if not text:
        raise ValueError("preview text cannot be empty")
    if len(text) > 600:
        raise ValueError("preview text must be 600 characters or fewer")
    if not voice_id.strip():
        raise ValueError("voice_id is required")

    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "speed": speed,
        },
    }
    try:
        with _client(settings) as client:
            response = client.post(
                f"/v1/text-to-speech/{voice_id}",
                params={"output_format": "mp3_44100_128"},
                headers={"Accept": "audio/mpeg"},
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ElevenLabsIntegrationError(
            f"ElevenLabs preview generation failed: {exc}"
        ) from exc

    audio = response.content
    if not audio:
        raise ElevenLabsIntegrationError(
            "ElevenLabs preview generation returned empty audio"
        )
    metadata: dict[str, object] = {
        "voice_id": voice_id,
        "model_id": model_id,
        "character_cost": response.headers.get("character-cost"),
        "request_id": response.headers.get("request-id"),
        "trace_id": response.headers.get("x-trace-id"),
        "output_format": "mp3_44100_128",
    }
    return audio, metadata
