from __future__ import annotations

from typing import Any

import httpx

from katcha.config import Settings, get_settings


class ElevenLabsIntegrationError(RuntimeError):
    pass


def _headers(settings: Settings) -> dict[str, str]:
    if not settings.elevenlabs_api_key:
        raise ElevenLabsIntegrationError("ElevenLabs API key is not configured")
    return {"xi-api-key": settings.elevenlabs_api_key}


def elevenlabs_status(settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    configured = bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id)
    if not configured:
        return {
            "configured": False,
            "connected": False,
            "voice_id": settings.elevenlabs_voice_id,
            "voice_name": None,
            "model_id": settings.elevenlabs_model_id,
            "output_format": settings.elevenlabs_output_format,
            "subscription": None,
            "detail": "Set KATCHA_ELEVENLABS_API_KEY and KATCHA_ELEVENLABS_VOICE_ID.",
        }

    timeout = httpx.Timeout(settings.elevenlabs_timeout_seconds)
    try:
        with httpx.Client(
            base_url="https://api.elevenlabs.io",
            headers=_headers(settings),
            timeout=timeout,
        ) as client:
            voice_response = client.get(f"/v1/voices/{settings.elevenlabs_voice_id}")
            voice_response.raise_for_status()
            subscription_response = client.get("/v1/user/subscription")
            subscription_response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ElevenLabsIntegrationError(
            f"ElevenLabs connection check failed: {exc}"
        ) from exc

    voice = voice_response.json()
    subscription = subscription_response.json()
    used = int(subscription.get("character_count") or 0)
    limit = int(subscription.get("character_limit") or 0)
    return {
        "configured": True,
        "connected": True,
        "voice_id": settings.elevenlabs_voice_id,
        "voice_name": voice.get("name"),
        "voice_category": voice.get("category"),
        "voice_labels": dict(voice.get("labels") or {}),
        "model_id": settings.elevenlabs_model_id,
        "output_format": settings.elevenlabs_output_format,
        "subscription": {
            "tier": subscription.get("tier"),
            "status": subscription.get("status"),
            "character_count": used,
            "character_limit": limit,
            "remaining_characters": max(0, limit - used),
            "next_reset_unix": subscription.get("next_character_count_reset_unix"),
            "can_extend_character_limit": subscription.get(
                "can_extend_character_limit"
            ),
        },
        "detail": "ElevenLabs API and configured voice are reachable.",
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
        with httpx.Client(
            base_url="https://api.elevenlabs.io",
            headers=_headers(settings),
            timeout=httpx.Timeout(settings.elevenlabs_timeout_seconds),
        ) as client:
            response = client.get("/v2/voices", params=params)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ElevenLabsIntegrationError(
            f"ElevenLabs voice search failed: {exc}"
        ) from exc

    payload = response.json()
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
