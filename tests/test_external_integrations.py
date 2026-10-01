import uuid
from types import SimpleNamespace

from katcha.api import integrations
from katcha.api.main import app
from katcha.services.external_edit import (
    _invideo_instructions,
    _next_production_render_generation,
    _next_short_episode_render_generation,
)


def test_external_provider_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/v1/integrations/providers",
        "/v1/integrations/elevenlabs/status",
        "/v1/integrations/elevenlabs/voices",
        "/v1/integrations/elevenlabs/models",
        "/v1/integrations/elevenlabs/channels/{channel_profile_id}",
        "/v1/integrations/elevenlabs/channels/{channel_profile_id}/enabled",
        "/v1/integrations/elevenlabs/channels/{channel_profile_id}/preview",
        "/v1/integrations/invideo/handoffs",
        "/v1/integrations/invideo/handoffs/{handoff_id}",
        "/v1/integrations/invideo/handoffs/{handoff_id}/manifest",
        "/v1/integrations/invideo/handoffs/{handoff_id}/package",
        "/v1/integrations/invideo/handoffs/{handoff_id}/output",
        "/v1/integrations/invideo/handoffs/{handoff_id}/adopt",
        "/v1/integrations/invideo/handoffs/{handoff_id}/metrics",
    }
    assert expected <= set(paths)
    assert "get" in paths["/v1/integrations/elevenlabs/status"]
    assert "get" in paths["/v1/integrations/elevenlabs/voices"]
    assert "get" in paths["/v1/integrations/elevenlabs/models"]
    assert "put" in paths["/v1/integrations/elevenlabs/channels/{channel_profile_id}"]
    assert "patch" in paths[
        "/v1/integrations/elevenlabs/channels/{channel_profile_id}/enabled"
    ]
    assert "post" in paths[
        "/v1/integrations/elevenlabs/channels/{channel_profile_id}/preview"
    ]
    assert "post" in paths["/v1/integrations/invideo/handoffs"]
    assert "post" in paths["/v1/integrations/invideo/handoffs/{handoff_id}/output"]
    assert "post" in paths["/v1/integrations/invideo/handoffs/{handoff_id}/adopt"]
    assert "post" in paths["/v1/integrations/invideo/handoffs/{handoff_id}/metrics"]


def test_elevenlabs_enable_toggle_preserves_saved_channel_config(monkeypatch) -> None:
    channel_id = uuid.uuid4()
    saved_config = {
        "voice_id": "voice-a",
        "model_id": "eleven_multilingual_v2",
        "saved_voices": [{"voice_id": "voice-a", "name": "Host"}],
    }
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        integrations,
        "get_channel_provider_setting",
        lambda *_args: SimpleNamespace(enabled=True, config=saved_config),
    )

    def capture_setting(_channel_id, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(integrations, "upsert_channel_provider_setting", capture_setting)
    monkeypatch.setattr(
        integrations,
        "_channel_elevenlabs_response",
        lambda _channel_id: integrations.ElevenLabsChannelConfigResponse(
            channel_profile_id=channel_id,
            enabled=False,
            voice_id="voice-a",
            voice_name="Host",
            model_id="eleven_multilingual_v2",
            model_name="Multilingual v2",
            saved_voices=[
                integrations.ElevenLabsSavedVoiceResponse(
                    voice_id="voice-a",
                    name="Host",
                )
            ],
            source="channel",
        ),
    )

    result = integrations.update_channel_elevenlabs_enabled(
        channel_id,
        integrations.ElevenLabsChannelEnabledUpdate(
            enabled=False,
            actor="channel-studio",
        ),
    )

    assert captured["enabled"] is False
    assert captured["config"] == saved_config
    assert captured["actor"] == "channel-studio"
    assert result.enabled is False


def test_disabled_elevenlabs_response_preserves_saved_voice_without_global_fallback(
    monkeypatch,
) -> None:
    channel_id = uuid.uuid4()
    monkeypatch.setattr(
        integrations,
        "get_channel_provider_setting",
        lambda *_args: SimpleNamespace(
            enabled=False,
            config={
                "voice_id": "saved-voice",
                "voice_name": "Saved Host",
                "model_id": "eleven_multilingual_v2",
                "model_name": "Multilingual v2",
                "saved_voices": [
                    {"voice_id": "saved-voice", "name": "Saved Host"}
                ],
            },
        ),
    )
    monkeypatch.setattr(
        integrations,
        "resolve_elevenlabs_voice",
        lambda **_kwargs: (None, "eleven_flash_v2_5"),
    )

    result = integrations._channel_elevenlabs_response(channel_id)

    assert result.enabled is False
    assert result.source == "channel"
    assert result.voice_id == "saved-voice"
    assert result.voice_name == "Saved Host"
    assert result.model_id == "eleven_multilingual_v2"
    assert [voice.voice_id for voice in result.saved_voices] == ["saved-voice"]


def test_invideo_brief_keeps_katcha_as_source_of_truth() -> None:
    instructions = _invideo_instructions(
        brand={"brand_key": "ranksnaxx"},
        blueprint={"key": "persona_commentary"},
    )
    text = " ".join(instructions).lower()
    assert "source of truth" in text
    assert "do not publish directly" in text
    assert "brand.json" in text
    assert "editing-recipe.json" in text



class _GenerationSession:
    def __init__(self, current: int | None) -> None:
        self.current = current

    def scalar(self, _statement: object) -> int | None:
        return self.current


def test_invideo_adoption_uses_next_render_generation() -> None:
    production_id = uuid.uuid4()
    episode_id = uuid.uuid4()

    assert _next_production_render_generation(
        _GenerationSession(None),
        production_id,
    ) == 1
    assert _next_production_render_generation(
        _GenerationSession(3),
        production_id,
    ) == 4
    assert _next_short_episode_render_generation(
        _GenerationSession(2),
        episode_id,
    ) == 3



def test_elevenlabs_channel_config_persists_voice_library(monkeypatch) -> None:
    channel_id = uuid.uuid4()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        integrations,
        "get_elevenlabs_voice",
        lambda voice_id: {
            "voice_id": voice_id,
            "name": {
                "default-voice": "Default Host",
                "host-a": "Long-form Host A",
                "host-b": "Long-form Host B",
            }[voice_id],
            "category": "generated",
            "labels": {"accent": "american"},
        },
    )
    monkeypatch.setattr(
        integrations,
        "list_elevenlabs_models",
        lambda: [{"model_id": "eleven_multilingual_v2", "name": "Multilingual v2"}],
    )

    def capture_setting(_channel_id, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(integrations, "upsert_channel_provider_setting", capture_setting)
    monkeypatch.setattr(
        integrations,
        "_channel_elevenlabs_response",
        lambda _channel_id, **kwargs: integrations.ElevenLabsChannelConfigResponse(
            channel_profile_id=channel_id,
            enabled=True,
            voice_id="default-voice",
            voice_name=kwargs.get("voice_name"),
            model_id="eleven_multilingual_v2",
            model_name=kwargs.get("model_name"),
            saved_voices=[
                integrations.ElevenLabsSavedVoiceResponse(
                    voice_id="default-voice",
                    name="Default Host",
                ),
                integrations.ElevenLabsSavedVoiceResponse(
                    voice_id="host-a",
                    name="Long-form Host A",
                ),
                integrations.ElevenLabsSavedVoiceResponse(
                    voice_id="host-b",
                    name="Long-form Host B",
                ),
            ],
            longform_primary_voice_id="host-a",
            longform_secondary_voice_id="host-b",
            source="channel",
        ),
    )

    result = integrations.update_channel_elevenlabs_config(
        channel_id,
        integrations.ElevenLabsChannelConfigUpdate(
            voice_id="default-voice",
            saved_voice_ids=["default-voice", "host-a", "host-b"],
            longform_primary_voice_id="host-a",
            longform_secondary_voice_id="host-b",
            model_id="eleven_multilingual_v2",
        ),
    )

    config = captured["config"]
    assert isinstance(config, dict)
    assert config["voice_id"] == "default-voice"
    assert config["longform_primary_voice_id"] == "host-a"
    assert config["longform_secondary_voice_id"] == "host-b"
    assert [voice["voice_id"] for voice in config["saved_voices"]] == [
        "default-voice",
        "host-a",
        "host-b",
    ]
    assert result.longform_secondary_voice_id == "host-b"
