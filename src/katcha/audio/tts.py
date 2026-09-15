from __future__ import annotations

import base64
import io
import math
import wave
from dataclasses import dataclass
from decimal import Decimal

from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import ModelTarget, assert_ai_budget
from katcha.config import Settings, get_settings


class TTSUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    key: str
    version: str
    provider: str
    model: str
    voice: str
    instructions: str


@dataclass(frozen=True, slots=True)
class TTSResult:
    audio: bytes
    content_type: str
    extension: str
    duration_seconds: float
    target: ModelTarget
    profile: VoiceProfile
    input_units: int
    output_units: int
    estimated_cost_usd: Decimal
    cost_metadata: dict[str, object]


VOICE_PROFILES: dict[str, VoiceProfile] = {
    "openai_youth_v1": VoiceProfile(
        key="openai_youth_v1",
        version="1",
        provider="openai",
        model="gpt-4o-mini-tts-2025-12-15",
        voice="ash",
        instructions=(
            "Sound like a young adult male internet host: upbeat, naturally amused, quick and "
            "conversational. Keep comedic timing crisp. Never shout, over-act, or sound like an "
            "advertisement. Read the supplied words exactly and do not add commentary."
        ),
    ),
    "gemini_youth_v1": VoiceProfile(
        key="gemini_youth_v1",
        version="1",
        provider="gemini",
        model="gemini-3.1-flash-tts-preview",
        voice="Puck",
        instructions=(
            "Speak as a young adult male internet host. Be upbeat, naturally amused, quick and "
            "conversational, with crisp comedic timing. Do not shout or sound promotional."
        ),
    ),
}


def get_voice_profile(key: str) -> VoiceProfile:
    try:
        return VOICE_PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"unknown voice profile: {key}") from exc


def choose_voice_profile(settings: Settings | None = None) -> VoiceProfile:
    settings = settings or get_settings()
    requested = get_voice_profile(settings.tts_profile)
    if requested.provider == "openai" and settings.openai_api_key:
        return requested
    if requested.provider == "gemini" and settings.gemini_api_key:
        return requested
    if settings.openai_api_key:
        return VOICE_PROFILES["openai_youth_v1"]
    if settings.gemini_api_key:
        return VOICE_PROFILES["gemini_youth_v1"]
    raise TTSUnavailable("no configured TTS provider is available")


def _wav_duration(audio: bytes) -> float:
    with wave.open(io.BytesIO(audio), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    if rate <= 0:
        raise ValueError("invalid WAV sample rate")
    return frames / rate


def _wrap_pcm_wav(pcm: bytes, *, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _binary_response_bytes(response: object) -> bytes:
    read = getattr(response, "read", None)
    if callable(read):
        data = read()
        if isinstance(data, bytes):
            return data
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    raise RuntimeError("speech provider returned an unsupported binary response")


def _estimated_text_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _openai_tts(text: str, profile: VoiceProfile, settings: Settings) -> TTSResult:
    if not settings.openai_api_key:
        raise TTSUnavailable("OpenAI API key is not configured")
    from openai import OpenAI

    assert_ai_budget(Decimal("0.05"))
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.audio.speech.create(
        model=profile.model,
        voice=profile.voice,
        input=text,
        instructions=profile.instructions,
        response_format="wav",
        speed=1.03,
    )
    audio = _binary_response_bytes(response)
    duration = _wav_duration(audio)
    target = ModelTarget("openai", profile.model)
    input_units = _estimated_text_tokens(text + profile.instructions)
    # OpenAI's speech endpoint does not expose request-level audio token usage. The public
    # ~$0.015/min estimate corresponds to about 1,250 output units/min at the listed rate.
    output_units = max(1, math.ceil(duration * (1250 / 60)))
    cost = estimate_token_cost(target, input_units, output_units)
    return TTSResult(
        audio=audio,
        content_type="audio/wav",
        extension="wav",
        duration_seconds=duration,
        target=target,
        profile=profile,
        input_units=input_units,
        output_units=output_units,
        estimated_cost_usd=cost,
        cost_metadata={
            "estimated_cost": True,
            "usage_basis": "public_estimated_0.015_usd_per_minute",
        },
    )


def _gemini_tts(text: str, profile: VoiceProfile, settings: Settings) -> TTSResult:
    if not settings.gemini_api_key:
        raise TTSUnavailable("Gemini API key is not configured")
    from google import genai

    assert_ai_budget(Decimal("0.05"))
    client = genai.Client(api_key=settings.gemini_api_key)
    prompt = f"{profile.instructions}\n\nRead exactly this text:\n{text}"
    interaction = client.interactions.create(
        model=profile.model,
        input=prompt,
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": profile.voice}]},
    )
    output_audio = getattr(interaction, "output_audio", None)
    encoded = getattr(output_audio, "data", None)
    if not isinstance(encoded, str):
        raise RuntimeError("Gemini TTS response did not contain audio data")
    pcm = base64.b64decode(encoded)
    audio = _wrap_pcm_wav(pcm)
    duration = _wav_duration(audio)
    target = ModelTarget("gemini", profile.model)
    input_units = _estimated_text_tokens(prompt)
    # Gemini documents 25 output audio tokens per second for this model.
    output_units = max(1, math.ceil(duration * 25))
    cost = estimate_token_cost(target, input_units, output_units)
    return TTSResult(
        audio=audio,
        content_type="audio/wav",
        extension="wav",
        duration_seconds=duration,
        target=target,
        profile=profile,
        input_units=input_units,
        output_units=output_units,
        estimated_cost_usd=cost,
        cost_metadata={
            "estimated_cost": True,
            "usage_basis": "25_audio_tokens_per_second_plus_estimated_text_tokens",
        },
    )


def synthesize_speech(
    text: str,
    *,
    profile: VoiceProfile | None = None,
    settings: Settings | None = None,
) -> TTSResult:
    settings = settings or get_settings()
    profile = profile or choose_voice_profile(settings)
    if not text.strip():
        raise ValueError("TTS text cannot be empty")
    if profile.provider == "openai":
        return _openai_tts(text.strip(), profile, settings)
    if profile.provider == "gemini":
        return _gemini_tts(text.strip(), profile, settings)
    raise TTSUnavailable(f"unsupported TTS provider: {profile.provider}")
