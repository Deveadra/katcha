import io
import wave
from decimal import Decimal
from types import SimpleNamespace

from katcha.ai.router import ModelTarget
from katcha.audio import tts
from katcha.audio.tts import choose_voice_profile, get_voice_profile, voice_profile_for_target
from katcha.config import Settings
from katcha.orchestration.production_activities import _brand_voice_profiles


def test_voice_profile_prefers_requested_legacy_openai_when_configured() -> None:
    settings = Settings(
        openai_api_key="test-openai",
        gemini_api_key="test-gemini",
        tts_profile="openai_youth_v1",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "openai_youth_v1"
    assert profile.provider == "openai"


def test_voice_profile_falls_back_to_current_brand_when_requested_provider_missing() -> None:
    settings = Settings(
        openai_api_key=None,
        gemini_api_key="test-gemini",
        tts_profile="openai_youth_v1",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "gemini_youth_v2"
    assert profile.provider == "gemini"


def test_default_settings_select_branded_openai_voice_v2() -> None:
    settings = Settings(
        openai_api_key="test-openai",
        gemini_api_key="test-gemini",
    )
    profile = choose_voice_profile(settings)
    assert profile.key == "openai_youth_v2"
    assert profile.version == "2"


def test_provider_target_prefers_latest_brand_direction() -> None:
    target = ModelTarget("openai", "gpt-4o-mini-tts-2025-12-15")
    assert voice_profile_for_target(target).key == "openai_youth_v2"


def test_legacy_voice_profiles_remain_addressable() -> None:
    assert get_voice_profile("openai_youth_v1").version == "1"
    assert get_voice_profile("gemini_youth_v1").version == "1"


def _test_wav(duration_seconds: float = 1.25, sample_rate: int = 8000) -> bytes:
    frames = int(duration_seconds * sample_rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def test_fixture_tts_writes_seekable_wav_file_for_reliable_duration(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, *, check, capture_output):
        commands.append(command)
        output_path = command[command.index("-w") + 1]
        with open(output_path, "wb") as handle:
            handle.write(_test_wav())
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    result = tts._fixture_tts("fixture narration")

    assert result.duration_seconds == 1.25
    assert result.estimated_cost_usd == 0
    assert commands
    assert "--stdout" not in commands[0]
    assert "-w" in commands[0]



def test_elevenlabs_tts_uses_public_api_and_tracks_character_cost(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        content = b"\x00\x00" * 24000
        headers = {
            "character-cost": "20",
            "request-id": "request-123",
            "x-trace-id": "trace-123",
        }

        def raise_for_status(self) -> None:
            return None

    def fake_post(url, *, params, headers, json, timeout):
        calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr(tts.httpx, "post", fake_post)
    settings = Settings(
        elevenlabs_api_key="secret-key",
        elevenlabs_voice_id="voice-123",
        elevenlabs_model_id="eleven_multilingual_v2",
        elevenlabs_output_format="pcm_24000",
        elevenlabs_usd_per_1000_credits=0.2,
    )
    profile = tts._resolve_profile(
        get_voice_profile("elevenlabs_rank_snaxx_v1"),
        settings,
    )

    result = tts._elevenlabs_tts("Hello RankSnaxx", profile, settings)

    assert result.profile.voice == "voice-123"
    assert result.target.provider == "elevenlabs"
    assert result.duration_seconds == 1.0
    assert result.input_units == 20
    assert result.estimated_cost_usd == Decimal("0.00400000")
    assert result.cost_metadata["request_id"] == "request-123"
    assert calls[0]["url"] == "https://api.elevenlabs.io/v1/text-to-speech/voice-123"
    assert calls[0]["params"] == {"output_format": "pcm_24000"}
    assert calls[0]["headers"]["xi-api-key"] == "secret-key"
    assert calls[0]["json"]["model_id"] == "eleven_multilingual_v2"
    assert calls[0]["json"]["voice_settings"]["speed"] == 1.03



def test_fixed_channel_voice_routing_preserves_elevenlabs_primary() -> None:
    settings = Settings(
        openai_api_key="test-openai",
        gemini_api_key="test-gemini",
        elevenlabs_api_key="test-elevenlabs",
        elevenlabs_voice_id="voice-123",
        ai_live_routing_mode="free_first",
    )
    primary, fallback = _brand_voice_profiles(
        {
            "voice_policy": {
                "routing_mode": "fixed",
                "preferred_profiles": [
                    "elevenlabs_rank_snaxx_v1",
                    "openai_youth_v2",
                    "gemini_youth_v2",
                ],
            }
        },
        settings,
    )

    assert primary is not None
    assert primary.provider == "elevenlabs"
    assert fallback is not None
    assert fallback.provider == "openai"


def test_inherited_channel_voice_routing_still_honors_free_first() -> None:
    settings = Settings(
        openai_api_key="test-openai",
        gemini_api_key="test-gemini",
        elevenlabs_api_key="test-elevenlabs",
        elevenlabs_voice_id="voice-123",
        ai_live_routing_mode="free_first",
    )
    primary, _ = _brand_voice_profiles(
        {
            "voice_policy": {
                "routing_mode": "inherit",
                "preferred_profiles": [
                    "elevenlabs_rank_snaxx_v1",
                    "openai_youth_v2",
                    "gemini_youth_v2",
                ],
            }
        },
        settings,
    )

    assert primary is not None
    assert primary.provider == "gemini"
