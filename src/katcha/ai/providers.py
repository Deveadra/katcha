from __future__ import annotations

import base64
import time
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import ModelTarget, assert_ai_budget, record_usage, route_for
from katcha.ai.schemas import ClipVisionResult, DeepVideoResult
from katcha.config import Settings, get_settings
from katcha.domain import AITask


class ProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AIResult:
    value: ClipVisionResult | DeepVideoResult
    target: ModelTarget
    input_tokens: int
    output_tokens: int


def _prompt(transcript: str | None, *, deep: bool) -> str:
    task = (
        "Analyze the complete source video, including temporal sequence and audio."
        if deep
        else "Analyze this chronological contact sheet sampled from one short video."
    )
    transcript_text = transcript.strip() if transcript else "(no usable transcript)"
    return (
        f"{task}\n"
        "You are a media classifier for an entertainment-video ranking system. "
        "Describe what actually happens without inventing missing events. Scores are 0-100 "
        "and should reflect the source material itself, not legal or policy judgments.\n\n"
        "Identify: event summary, broad categories, tone, setup, payoff, hook strength, "
        "surprise, humor, comment potential, rewatch potential, whether missing temporal "
        "or audio context prevents confident understanding, and confidence.\n\n"
        "Set requires_deep_video=true only when the sampled frames and transcript are "
        "genuinely insufficient to understand the event or payoff. For complete-video "
        "analysis, set it false.\n\n"
        f"Transcript:\n{transcript_text}\n"
    )


def _record(
    *,
    task: AITask,
    target: ModelTarget,
    input_tokens: int,
    output_tokens: int,
    reference_id: str,
) -> None:
    cost = estimate_token_cost(target, input_tokens, output_tokens)
    record_usage(
        task=task,
        target=target,
        input_units=input_tokens,
        output_units=output_tokens,
        cost_usd=cost,
        reference_type="analysis_run",
        reference_id=reference_id,
        metadata={
            "estimated_cost": True,
            "pricing_basis": "public_paid_rate_2026-09-15",
        },
    )


def _openai_contact_sheet(
    image_bytes: bytes,
    transcript: str | None,
    target: ModelTarget,
    settings: Settings,
    reference_id: str,
) -> AIResult:
    if not settings.openai_api_key:
        raise ProviderUnavailable("OpenAI API key is not configured")
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    image_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
    response = client.responses.create(
        model=target.model,
        store=False,
        reasoning={"effort": "low"},
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _prompt(transcript, deep=False)},
                    {"type": "input_image", "image_url": image_url, "detail": "low"},
                ],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "clip_vision",
                "schema": ClipVisionResult.model_json_schema(),
                "strict": False,
            }
        },
        max_output_tokens=1200,
    )
    value = ClipVisionResult.model_validate_json(response.output_text)
    usage = response.usage
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    _record(
        task=AITask.BULK_VISION,
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reference_id=reference_id,
    )
    return AIResult(value, target, input_tokens, output_tokens)


def _gemini_contact_sheet(
    image_bytes: bytes,
    transcript: str | None,
    target: ModelTarget,
    settings: Settings,
    reference_id: str,
) -> AIResult:
    if not settings.gemini_api_key:
        raise ProviderUnavailable("Gemini API key is not configured")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=target.model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            _prompt(transcript, deep=False),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ClipVisionResult,
        ),
    )
    value = ClipVisionResult.model_validate_json(response.text)
    usage = response.usage_metadata
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    _record(
        task=AITask.BULK_VISION,
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reference_id=reference_id,
    )
    return AIResult(value, target, input_tokens, output_tokens)


def analyze_contact_sheet(
    image_bytes: bytes,
    transcript: str | None,
    *,
    reference_id: str,
    settings: Settings | None = None,
) -> AIResult:
    settings = settings or get_settings()
    assert_ai_budget(Decimal("0.01"))
    route = route_for(AITask.BULK_VISION)
    if route.primary.provider == "openai" and settings.openai_api_key:
        return _openai_contact_sheet(
            image_bytes,
            transcript,
            route.primary,
            settings,
            reference_id,
        )
    if route.fallback and route.fallback.provider == "gemini" and settings.gemini_api_key:
        return _gemini_contact_sheet(
            image_bytes,
            transcript,
            route.fallback,
            settings,
            reference_id,
        )
    raise ProviderUnavailable("no configured provider is available for bulk vision")


def analyze_full_video(
    video_path: Path,
    transcript: str | None,
    *,
    reference_id: str,
    settings: Settings | None = None,
) -> AIResult:
    settings = settings or get_settings()
    assert_ai_budget(Decimal("0.10"))
    route = route_for(AITask.DEEP_VIDEO)
    target = route.primary
    if target.provider != "gemini" or not settings.gemini_api_key:
        raise ProviderUnavailable("Gemini is required for native full-video escalation")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    uploaded = client.files.upload(file=video_path)
    try:
        deadline = time.monotonic() + 180
        while not uploaded.state or uploaded.state.name != "ACTIVE":
            if uploaded.state and uploaded.state.name == "FAILED":
                raise RuntimeError("Gemini file processing failed")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Gemini video processing did not become ACTIVE within 180 seconds"
                )
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)

        response = client.models.generate_content(
            model=target.model,
            contents=[uploaded, _prompt(transcript, deep=True)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DeepVideoResult,
            ),
        )
        value = DeepVideoResult.model_validate_json(response.text)
        usage = response.usage_metadata
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
            getattr(usage, "thoughts_token_count", 0) or 0
        )
        _record(
            task=AITask.DEEP_VIDEO,
            target=target,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reference_id=reference_id,
        )
        return AIResult(value, target, input_tokens, output_tokens)
    finally:
        with suppress(Exception):
            client.files.delete(name=uploaded.name)
