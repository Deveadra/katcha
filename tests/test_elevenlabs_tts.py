import io
import wave
from decimal import Decimal

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
        elevenlabs_model_id="eleven_multilingual_v2",
    )
    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", "eleven_multilingual_v2"),
    )
    assert profile.key == "elevenlabs_rank_snaxx_v1"
    assert profile.provider == "elevenlabs"
    assert profile.model == "eleven_multilingual_v2"
    assert profile.voice == "rank-snaxx-voice"


def test_elevenlabs_tts_records_provider_credits_and_request_ids(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        content = _pcm()
        headers = {
            "character-cost": "7",
            "request-id": "req-1",
            "x-trace-id": "trace-1",
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
        elevenlabs_api_key="test-eleven",
        elevenlabs_voice_id="rank-snaxx-voice",
        elevenlabs_model_id="eleven_multilingual_v2",
        elevenlabs_output_format="pcm_24000",
        elevenlabs_usd_per_1000_credits=0.2,
    )
    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", "eleven_multilingual_v2"),
    )

    result = tts._elevenlabs_tts("RankSnaxx test", profile, settings)

    assert result.target == ModelTarget("elevenlabs", "eleven_multilingual_v2")
    assert result.profile.voice == "rank-snaxx-voice"
    assert result.input_units == 7
    assert result.estimated_cost_usd == Decimal("0.00140000")
    assert result.cost_metadata["reported_credits"] == "7"
    assert result.cost_metadata["request_id"] == "req-1"
    assert result.cost_metadata["trace_id"] == "trace-1"
    with wave.open(io.BytesIO(result.audio), "rb") as handle:
        assert handle.getframerate() == 24000
        assert handle.getnchannels() == 1

    assert calls[0]["url"].endswith("/v1/text-to-speech/rank-snaxx-voice")
    assert calls[0]["params"] == {"output_format": "pcm_24000"}
    assert calls[0]["json"]["model_id"] == "eleven_multilingual_v2"


def test_elevenlabs_leaves_usd_unestimated_without_plan_rate(monkeypatch) -> None:
    class Response:
        content = _pcm(0.5)
        headers = {"character-cost": "11"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(tts.httpx, "post", lambda *args, **kwargs: Response())
    settings = Settings(
        elevenlabs_api_key="test-eleven",
        elevenlabs_voice_id="rank-snaxx-voice",
        elevenlabs_usd_per_1000_credits=0,
    )
    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", settings.elevenlabs_model_id),
    )
    result = tts._elevenlabs_tts("test narration", profile, settings)
    assert result.estimated_cost_usd == Decimal("0E-8")
    assert result.cost_metadata["estimated_cost"] is False
    assert result.cost_metadata["reported_credits"] == "11"


def test_elevenlabs_requires_pcm_for_deterministic_timing() -> None:
    settings = Settings(
        elevenlabs_api_key="test-eleven",
        elevenlabs_voice_id="rank-snaxx-voice",
        elevenlabs_output_format="mp3_44100_128",
    )
    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", settings.elevenlabs_model_id),
    )
    try:
        tts._elevenlabs_tts("test", profile, settings)
    except tts.TTSUnavailable as exc:
        assert "PCM output" in str(exc)
    else:
        raise AssertionError("expected non-PCM ElevenLabs output to be rejected")



def test_elevenlabs_voice_role_reaches_channel_resolver(monkeypatch) -> None:
    calls: list[str | None] = []

    def resolve(*, channel_profile_id=None, settings=None, role=None):
        calls.append(role)
        return (
            "longform-host-a" if role == "longform_primary" else "default-host",
            "eleven_multilingual_v2",
        )

    monkeypatch.setattr(tts, "resolve_elevenlabs_voice", resolve)
    settings = Settings(
        elevenlabs_api_key="test-eleven",
        elevenlabs_voice_id="global-fallback",
        elevenlabs_model_id="eleven_multilingual_v2",
    )

    profile = choose_voice_profile(
        settings,
        target=ModelTarget("elevenlabs", "eleven_multilingual_v2"),
        voice_role="longform_primary",
    )

    assert profile.voice == "longform-host-a"
    assert calls == ["longform_primary"]
