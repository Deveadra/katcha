from __future__ import annotations

from katcha.config import Settings
from katcha.services import elevenlabs_integration as integration


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _Client:
    def __init__(self, *, responses: dict[str, dict[str, object]], **_: object) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, path: str, params: dict[str, object] | None = None) -> _Response:
        self.calls.append((path, params))
        return _Response(self.responses[path])


def test_elevenlabs_status_is_safe_when_unconfigured() -> None:
    settings = Settings(elevenlabs_api_key=None, elevenlabs_voice_id=None)
    result = integration.elevenlabs_status(settings)

    assert result["configured"] is False
    assert result["connected"] is False
    assert "API_KEY" in str(result["detail"])
    assert "secret" not in str(result).lower()


def test_elevenlabs_status_returns_voice_and_subscription_without_secret(
    monkeypatch,
) -> None:
    responses = {
        "/v1/voices/ranksnaxx-voice": {
            "voice_id": "ranksnaxx-voice",
            "name": "RankSnaxx",
            "category": "generated",
            "labels": {"accent": "american", "age": "young"},
        },
        "/v1/user/subscription": {
            "tier": "starter",
            "status": "active",
            "character_count": 1234,
            "character_limit": 10000,
            "next_character_count_reset_unix": 1800000000,
            "can_extend_character_limit": True,
        },
    }

    monkeypatch.setattr(
        integration.httpx,
        "Client",
        lambda **kwargs: _Client(responses=responses, **kwargs),
    )
    settings = Settings(
        elevenlabs_api_key="super-secret-key",
        elevenlabs_voice_id="ranksnaxx-voice",
    )

    result = integration.elevenlabs_status(settings)

    assert result["connected"] is True
    assert result["voice_name"] == "RankSnaxx"
    assert result["subscription"]["tier"] == "starter"
    assert result["subscription"]["remaining_characters"] == 8766
    assert "super-secret-key" not in str(result)


def test_elevenlabs_voice_search_returns_operator_safe_fields(monkeypatch) -> None:
    responses = {
        "/v2/voices": {
            "voices": [
                {
                    "voice_id": "voice-1",
                    "name": "Calm Host",
                    "category": "professional",
                    "description": "Expressive social voice",
                    "labels": {"accent": "american"},
                    "preview_url": "https://example.invalid/preview.mp3",
                    "is_owner": True,
                    "is_legacy": False,
                    "internal_secret": "never-return-this",
                }
            ],
            "has_more": False,
            "total_count": 1,
            "next_page_token": None,
        }
    }

    monkeypatch.setattr(
        integration.httpx,
        "Client",
        lambda **kwargs: _Client(responses=responses, **kwargs),
    )
    settings = Settings(
        elevenlabs_api_key="super-secret-key",
        elevenlabs_voice_id="voice-1",
    )

    result = integration.search_elevenlabs_voices(
        search="host",
        page_size=25,
        settings=settings,
    )

    assert result["total_count"] == 1
    assert result["voices"][0]["voice_id"] == "voice-1"
    assert result["voices"][0]["name"] == "Calm Host"
    assert "internal_secret" not in result["voices"][0]
    assert "super-secret-key" not in str(result)
