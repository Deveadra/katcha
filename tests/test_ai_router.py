import uuid
from types import SimpleNamespace

from katcha.ai import router
from katcha.ai.router import route_for
from katcha.config import Settings
from katcha.domain import AITask


def test_deep_video_prefers_gemini() -> None:
    route = route_for(AITask.DEEP_VIDEO)
    assert route.primary.provider == "gemini"


def test_short_script_prefers_free_provider_by_default() -> None:
    route = route_for(AITask.SHORT_SCRIPT)
    assert route.primary.provider == "gemini"
    assert route.fallback is not None
    assert route.fallback.provider == "openai"


def test_elevenlabs_target_uses_channel_provider_setting(monkeypatch) -> None:
    channel_id = uuid.uuid4()
    monkeypatch.setattr(
        router,
        "get_settings",
        lambda: Settings(
            elevenlabs_api_key="test-elevenlabs",
            elevenlabs_voice_id="global-voice",
            elevenlabs_model_id="eleven_multilingual_v2",
        ),
    )
    monkeypatch.setattr(
        router,
        "get_channel_provider_setting",
        lambda requested_channel_id, provider: SimpleNamespace(
            enabled=True,
            config={
                "voice_id": "rank-snaxx-voice",
                "model_id": "eleven_v4_turbo",
            },
        )
        if requested_channel_id == channel_id and provider == "elevenlabs"
        else None,
    )

    target = router._elevenlabs_target(channel_id)

    assert target is not None
    assert target.provider == "elevenlabs"
    assert target.model == "eleven_v4_turbo"
