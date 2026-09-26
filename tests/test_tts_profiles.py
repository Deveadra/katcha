import io
import wave
from types import SimpleNamespace

from katcha.ai.router import ModelTarget
from katcha.audio import tts
from katcha.audio.tts import choose_voice_profile, get_voice_profile, voice_profile_for_target
from katcha.config import Settings


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
