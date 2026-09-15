from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    assert_ai_budget,
    record_usage,
    route_for,
    route_for_channel,
)
from katcha.config import Settings, get_settings
from katcha.domain import AITask
from katcha.editorial.personas import HostPersona
from katcha.editorial.schemas import ShortScriptSet


class ScriptProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScriptGenerationResult:
    scripts: ShortScriptSet
    target: ModelTarget
    input_tokens: int
    output_tokens: int


def _active_ai_features(snapshot: dict[str, Any]) -> dict[str, Any]:
    ai_features = snapshot.get("ai_features")
    if not isinstance(ai_features, dict):
        return {}
    deep = ai_features.get("deep")
    bulk = ai_features.get("bulk")
    if isinstance(deep, dict):
        return deep
    if isinstance(bulk, dict):
        return bulk
    return {}


def build_script_prompt(
    persona: HostPersona,
    snapshot: dict[str, Any],
    *,
    prompt_version: str,
) -> str:
    ai = _active_ai_features(snapshot)
    duration = float(snapshot.get("duration_seconds") or 0)
    transcript = snapshot.get("transcript") or "(no transcript)"
    event_summary = ai.get("event_summary") or "Use the transcript/context conservatively."
    setup = ai.get("setup") or ""
    payoff = ai.get("payoff") or ""

    return (
        f"Prompt version: {prompt_version}\n"
        f"Host persona: {persona.key} {persona.version}\n"
        f"Audience: {persona.audience}\n"
        f"Identity: {persona.identity}\n"
        f"Delivery: {persona.delivery}\n"
        f"Comedy tools: {', '.join(persona.comedy)}\n"
        f"Avoid: {', '.join(persona.avoid)}\n"
        f"Interaction rule: {persona.interaction_style}\n\n"
        f"Source duration: {duration:.2f} seconds\n"
        f"Event: {event_summary}\n"
        f"Setup: {setup}\n"
        f"Payoff: {payoff}\n"
        f"Transcript: {transcript}\n\n"
        "Create exactly three distinct host treatments: observational, sarcastic, and "
        "interactive. The host must add a new joke, perspective, framing, or decision for "
        "the audience; never merely describe visible action. Keep speech economical so the "
        "source clip remains the star. Use pre and post commentary by default. Use mid only "
        "when interruption materially improves the joke, and provide the exact source time. "
        "If using an interaction prompt, place it after the payoff and make it a natural "
        "specific judgment, ranking, or choice rather than generic engagement bait."
    )


def _record_usage(
    target: ModelTarget,
    input_tokens: int,
    output_tokens: int,
    production_id: str,
) -> None:
    record_usage(
        task=AITask.SHORT_SCRIPT,
        target=target,
        input_units=input_tokens,
        output_units=output_tokens,
        cost_usd=estimate_token_cost(target, input_tokens, output_tokens),
        reference_type="production",
        reference_id=production_id,
        metadata={
            "estimated_cost": True,
            "pricing_basis": "public_paid_rate_2026-09-15",
        },
    )


def _openai_generate(
    prompt: str,
    target: ModelTarget,
    settings: Settings,
    production_id: str,
) -> ScriptGenerationResult:
    if not settings.openai_api_key:
        raise ScriptProviderUnavailable("OpenAI API key is not configured")
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=target.model,
        store=False,
        reasoning={"effort": "medium"},
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "short_script_set",
                "schema": ShortScriptSet.model_json_schema(),
                "strict": False,
            }
        },
        max_output_tokens=1800,
    )
    scripts = ShortScriptSet.model_validate_json(response.output_text)
    usage = response.usage
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    _record_usage(target, input_tokens, output_tokens, production_id)
    return ScriptGenerationResult(scripts, target, input_tokens, output_tokens)


def _gemini_generate(
    prompt: str,
    target: ModelTarget,
    settings: Settings,
    production_id: str,
) -> ScriptGenerationResult:
    if not settings.gemini_api_key:
        raise ScriptProviderUnavailable("Gemini API key is not configured")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=target.model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ShortScriptSet,
        ),
    )
    scripts = ShortScriptSet.model_validate_json(response.text)
    usage = response.usage_metadata
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    _record_usage(target, input_tokens, output_tokens, production_id)
    return ScriptGenerationResult(scripts, target, input_tokens, output_tokens)


def generate_short_scripts(
    persona: HostPersona,
    snapshot: dict[str, Any],
    *,
    prompt_version: str,
    production_id: str,
    channel_profile_id: uuid.UUID | None = None,
    expected_value: float = 0.5,
    settings: Settings | None = None,
) -> ScriptGenerationResult:
    settings = settings or get_settings()
    estimated_increment = Decimal("0.05")
    assert_ai_budget(estimated_increment)
    if channel_profile_id is not None:
        route = route_for_channel(
            AITask.SHORT_SCRIPT,
            channel_profile_id,
            estimated_increment_usd=estimated_increment,
            expected_value=expected_value,
        ).route
    else:
        route = route_for(AITask.SHORT_SCRIPT)
    prompt = build_script_prompt(persona, snapshot, prompt_version=prompt_version)

    if route.primary.provider == "openai" and settings.openai_api_key:
        return _openai_generate(prompt, route.primary, settings, production_id)
    if route.primary.provider == "gemini" and settings.gemini_api_key:
        return _gemini_generate(prompt, route.primary, settings, production_id)
    if route.fallback and route.fallback.provider == "openai" and settings.openai_api_key:
        return _openai_generate(prompt, route.fallback, settings, production_id)
    if route.fallback and route.fallback.provider == "gemini" and settings.gemini_api_key:
        return _gemini_generate(prompt, route.fallback, settings, production_id)
    raise ScriptProviderUnavailable("no configured provider is available for short scripting")
