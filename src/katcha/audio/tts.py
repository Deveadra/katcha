from __future__ import annotations

import base64
import io
import math
import uuid
import wave
from dataclasses import dataclass
from decimal import Decimal

from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    assert_ai_budget,
    record_usage,
    release_budget_reservation,
    route_for_channel,
)
from katcha.config import Settings, get_settings
from katcha.domain import AITask


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
    "openai_youth_v2": VoiceProfile(
        key="openai_youth_v2",
        version="2",
        provider="openai",
        model="gpt-4o-mini-tts-2025-12-15",
        voice="ash",
        instructions=(
            "Sound like a funny, energetic, down-to-earth late-teen or young-adult male friend, "
            "not an announcer. Be naturally amused and conversational, with a relaxed bright "
            "tone, crisp comedic timing, and real dynamic range. Normal energy should feel "
            "lively rather than loud. Use brief dry flattening for fake-serious jokes and small "
            "pauses when they improve timing. Let genuinely exciting moments lift naturally, "
            "but never maintain permanent hype. Do not shout, over-act, sound promotional, or "
            "perform a caricature of teenage slang. Read the supplied words exactly and do not "
            "add commentary."
        ),
    ),
    "gemini_youth_v2": VoiceProfile(
        key="gemini_youth_v2",
        version="2",
        provider="gemini",
        model="gemini-3.1-flash-tts-preview",
        voice="Puck",
        instructions=(
            "Speak like a funny, energetic, down-to-earth late-teen or young-adult male friend, "
            "not an announcer. Be naturally amused, conversational, and quick, with relaxed "
            "articulation, crisp comedic timing, and real dynamic range. Keep normal energy "
            "lively rather than loud; use brief dry delivery for fake-serious jokes and allow "
            "earned excitement without permanent hype. Never shout, sound promotional, or "
            "perform a caricature of teenage slang. Read only the supplied wording."
        ),
    ),
}


LATEST_VOICE_PROFILE_BY_PROVIDER: dict[str, str] = {
    "openai": "openai_youth_v2",
    "gemini": "gemini_youth_v2",
}


def get_voice_profile(key: str) -> VoiceProfile:
    try:
        return VOICE_PROFILES[key]
    except KeyError as exc:
        raise ValueError(f"unknown voice profile: {key}") from exc


def _target_for_profile(profile: VoiceProfile) -> ModelTarget:
    return ModelTarget(profile.provider, profile.model)


def voice_profile_for_target(target: ModelTarget) -> VoiceProfile:
    preferred_key = LATEST_VOICE_PROFILE_BY_PROVIDER.get(target.provider)
    if preferred_key is not None:
        preferred = VOICE_PROFILES[preferred_key]
        if preferred.model == target.model:
            return preferred
    for profile in VOICE_PROFILES.values():
        if profile.provider == target.provider and profile.model == target.model:
            return profile
    raise TTSUnavailable(
        f"no voice profile is configured for {target.provider}/{target.model}"
    )


def choose_voice_profile(
    settings: Settings | None = None,
    *,
    target: ModelTarget | None = None,
) -> VoiceProfile:
    settings = settings or get_settings()
    if target is not None:
        profile = voice_profile_for_target(target)
        if profile.provider == "openai" and not settings.openai_api_key:
            raise TTSUnavailable("OpenAI TTS provider is not configured")
        if profile.provider == "gemini" and not settings.gemini_api_key:
            raise TTSUnavailable("Gemini TTS provider is not configured")
        return profile

    requested = get_voice_profile(settings.tts_profile)
    if requested.provider == "openai" and settings.openai_api_key:
        return requested
    if requested.provider == "gemini" and settings.gemini_api_key:
        return requested
    if settings.openai_api_key:
        return VOICE_PROFILES["openai_youth_v2"]
    if settings.gemini_api_key:
        return VOICE_PROFILES["gemini_youth_v2"]
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
    target = _target_for_profile(profile)
    input_units = _estimated_text_tokens(text + profile.instructions)
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
    target = _target_for_profile(profile)
    input_units = _estimated_text_tokens(prompt)
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
    channel_profile_id: uuid.UUID | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    reservation_key: str | None = None,
    expected_value: float = 0.5,
    usage_metadata: dict[str, object] | None = None,
) -> TTSResult:
    settings = settings or get_settings()
    text = text.strip()
    if not text:
        raise ValueError("TTS text cannot be empty")

    estimated_increment = Decimal("0.05")
    assert_ai_budget(estimated_increment)
    reservation_id: uuid.UUID | None = None
    if channel_profile_id is not None:
        preferred_target = _target_for_profile(profile) if profile is not None else None
        decision = route_for_channel(
            AITask.TTS,
            channel_profile_id,
            estimated_increment_usd=estimated_increment,
            expected_value=expected_value,
            reference_type=reference_type,
            reference_id=reference_id,
            reservation_key=reservation_key,
            preferred_target=preferred_target,
        )
        reservation_id = decision.reservation_id
        profile = profile or choose_voice_profile(
            settings,
            target=decision.route.primary,
        )
    else:
        profile = profile or choose_voice_profile(settings)

    try:
        if profile.provider == "openai":
            result = _openai_tts(text, profile, settings)
        elif profile.provider == "gemini":
            result = _gemini_tts(text, profile, settings)
        else:
            raise TTSUnavailable(f"unsupported TTS provider: {profile.provider}")
    except Exception as exc:
        release_budget_reservation(
            reservation_id,
            reason=f"tts_failed:{type(exc).__name__}",
        )
        raise

    metadata = {
        "estimated_cost": True,
        "voice_profile": result.profile.key,
        **dict(result.cost_metadata or {}),
        **dict(usage_metadata or {}),
    }
    record_usage(
        task=AITask.TTS,
        target=result.target,
        input_units=result.input_units,
        output_units=result.output_units,
        cost_usd=result.estimated_cost_usd,
        reference_type=reference_type,
        reference_id=reference_id,
        metadata=metadata,
        reservation_id=reservation_id,
    )
    return result
