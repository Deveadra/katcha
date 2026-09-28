import io
import wave
from decimal import Decimal
from types import SimpleNamespace

from katcha.ai.router import ModelTarget
from katcha.audio import tts
from katcha.audio.tts import choose_voice_profile
from katcha.config import Settings


def _pcm(seconds: float = 1.0, rate: int = 24000) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def test_elevenlabs_target_resolves_configured_voice_and_model() -> None:
    settings = Settings(
        elevenlabs_api_key="test-eleven",
        elevenlabs_voice_id="rank-snaxx-voice",
        elevenlabs_model_id="eleven_flash_v2_5",
    )
    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", "eleven_flash_v2_5"),
    )
    assert profile.key == "elevenlabs_rank_snaxx_v1"
    assert profile.provider == "elevenlabs"
    assert profile.model == "eleven_flash_v2_5"
    assert profile.voice == "rank-snaxx-voice"


def test_elevenlabs_tts_posts_documented_payload_and_wraps_pcm(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        content = _pcm()

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
        elevenlabs_api_key="test-eleven",
        elevenlabs_voice_id="rank-snaxx-voice",
        elevenlabs_model_id="eleven_v3",
        elevenlabs_output_format="pcm_24000",
    )
    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", "eleven_v3"),
    )

    result = tts._elevenlabs_tts("RankSnaxx test", profile, settings)

    assert result.target == ModelTarget("elevenlabs", "eleven_v3")
    assert result.profile.voice == "rank-snaxx-voice"
    assert result.content_type == "audio/wav"
    assert result.extension == "wav"
    assert result.duration_seconds == 1.0
    assert result.input_units == len("RankSnaxx test")
    assert result.estimated_cost_usd == (
        Decimal(len("RankSnaxx test")) / Decimal("1000") * Decimal("0.10")
    ).quantize(Decimal("0.00000001"))
    with wave.open(io.BytesIO(result.audio), "rb") as handle:
        assert handle.getframerate() == 24000
        assert handle.getnchannels() == 1

    assert calls == [
        {
            "url": "https://api.elevenlabs.io/v1/text-to-speech/rank-snaxx-voice",
            "params": {"output_format": "pcm_24000"},
            "headers": {
                "xi-api-key": "test-eleven",
                "Content-Type": "application/json",
                "Accept": "application/octet-stream",
            },
            "json": {
                "text": "RankSnaxx test",
                "model_id": "eleven_v3",
                "voice_settings": {
                    "stability": 0.42,
                    "speed": 1.03,
                },
            },
            "timeout": 90,
        }
    ]


def test_elevenlabs_requires_pcm_for_deterministic_timing() -> None:
    settings = Settings(
        elevenlabs_api_key="test-eleven",
        elevenlabs_voice_id="rank-snaxx-voice",
        elevenlabs_output_format="mp3_44100_128",
    )
    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", "eleven_v3"),
    )

    try:
        tts._elevenlabs_tts("test", profile, settings)
    except tts.TTSUnavailable as exc:
        assert "PCM output format" in str(exc)
    else:
        raise AssertionError("expected non-PCM ElevenLabs output to be rejected")
