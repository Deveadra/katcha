from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from katcha.ai.failover import safe_to_fail_over
from katcha.ai.fixtures import fixture_short_scripts
from katcha.ai.gemini_capacity import run_with_gemini_capacity_fallback
from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    assert_ai_budget,
    record_usage,
    release_budget_reservation,
    route_for,
    route_for_channel,
)
from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.domain import AITask
from katcha.editorial.personas import HostPersona
from katcha.editorial.schemas import ShortScriptSet
from katcha.production_models import Production


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


def _routing_context(
    production_id: str,
    snapshot: dict[str, Any],
    channel_profile_id: uuid.UUID | None,
    expected_value: float | None,
) -> tuple[uuid.UUID | None, float]:
    resolved_profile = channel_profile_id
    if resolved_profile is None:
        with session_scope() as session:
            production = session.get(Production, uuid.UUID(production_id))
            if production is not None:
                resolved_profile = production.channel_profile_id
    if expected_value is None:
        try:
            raw = float(snapshot.get("candidate_score") or 0) / 100.0
        except (TypeError, ValueError):
            raw = 0.5
        expected_value = max(0.0, min(1.0, raw))
    return resolved_profile, expected_value


def _persona_rule_line(label: str, values: tuple[str, ...]) -> str:
    if not values:
        return ""
    return f"{label}: {' | '.join(values)}\n"


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
        f"Interaction rule: {persona.interaction_style}\n"
        f"{_persona_rule_line('Emotional range', persona.emotional_range)}"
        f"{_persona_rule_line('Hook rules', persona.hook_rules)}"
        f"{_persona_rule_line('Language rules', persona.language_rules)}"
        f"{_persona_rule_line('Trust rules', persona.trust_rules)}"
        f"{_persona_rule_line('Allowed interaction rituals', persona.interaction_rituals)}\n"
        f"Source duration: {duration:.2f} seconds\n"
        f"Event: {event_summary}\n"
        f"Setup: {setup}\n"
        f"Payoff: {payoff}\n"
        f"Transcript: {transcript}\n\n"
        "Create exactly three distinct host treatments: observational, sarcastic, and "
        "interactive. They must sound like the same host, not three different personalities. "
        "The host must add a new joke, perspective, framing, or decision for the audience; "
        "never merely describe visible action. Keep speech economical so the source clip "
        "remains the star. One precise line is better than several generic reactions. "
        "Use pre and post commentary by default. Use mid only when interruption materially "
        "improves the joke, prediction, comprehension, or callback, and provide the exact "
        "source time. A strong native source opening may justify no pre narration. If using an "
        "interaction prompt, place it after the payoff and make it a natural specific judgment, "
        "ranking, prediction, score, appeal, or choice rather than generic engagement bait. "
        "Never invent a factual detail, motive, consequence, or reveal that the supplied source "
        "context does not establish."
    )


def _record_usage(
    target: ModelTarget,
    input_tokens: int,
    output_tokens: int,
    production_id: str,
    reservation_id: uuid.UUID | None,
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
        reservation_id=reservation_id,
    )


def _openai_generate(
    prompt: str,
    target: ModelTarget,
    settings: Settings,
    production_id: str,
    reservation_id: uuid.UUID | None,
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
    usage = response.usage
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    _record_usage(
        target,
        input_tokens,
        output_tokens,
        production_id,
        reservation_id,
    )
    scripts = ShortScriptSet.model_validate_json(response.output_text)
    return ScriptGenerationResult(scripts, target, input_tokens, output_tokens)


def _gemini_generate(
    prompt: str,
    target: ModelTarget,
    settings: Settings,
    production_id: str,
    reservation_id: uuid.UUID | None,
) -> ScriptGenerationResult:
    if not settings.gemini_api_key:
        raise ScriptProviderUnavailable("Gemini API key is not configured")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)

    def invoke(candidate: ModelTarget) -> ScriptGenerationResult:
        response = client.models.generate_content(
            model=candidate.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ShortScriptSet,
            ),
        )
        usage = response.usage_metadata
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
            getattr(usage, "thoughts_token_count", 0) or 0
        )
        _record_usage(
            candidate,
            input_tokens,
            output_tokens,
            production_id,
            reservation_id,
        )
        scripts = ShortScriptSet.model_validate_json(response.text)
        return ScriptGenerationResult(
            scripts,
            candidate,
            input_tokens,
            output_tokens,
        )

    return run_with_gemini_capacity_fallback(target, invoke)


def generate_short_scripts(
    persona: HostPersona,
    snapshot: dict[str, Any],
    *,
    prompt_version: str,
    production_id: str,
    channel_profile_id: uuid.UUID | None = None,
    expected_value: float | None = None,
    settings: Settings | None = None,
) -> ScriptGenerationResult:
    settings = settings or get_settings()
    if settings.resolved_ai_execution_mode() == "fixture":
        return ScriptGenerationResult(
            fixture_short_scripts(),
            ModelTarget("fixture", "deterministic-short-script-v1"),
            0,
            0,
        )
    estimated_increment = Decimal("0.05")
    assert_ai_budget(estimated_increment)
    channel_profile_id, expected_value = _routing_context(
        production_id,
        snapshot,
        channel_profile_id,
        expected_value,
    )
    reservation_id: uuid.UUID | None = None
    if channel_profile_id is not None:
        decision = route_for_channel(
            AITask.SHORT_SCRIPT,
            channel_profile_id,
            estimated_increment_usd=estimated_increment,
            expected_value=expected_value,
            reference_type="production",
            reference_id=production_id,
            reservation_key=f"short-script:{production_id}",
        )
        route = decision.route
        reservation_id = decision.reservation_id
    else:
        route = route_for(AITask.SHORT_SCRIPT)
    prompt = build_script_prompt(persona, snapshot, prompt_version=prompt_version)

    def generate_for(target: ModelTarget) -> ScriptGenerationResult:
        if target.provider == "openai" and settings.openai_api_key:
            return _openai_generate(
                prompt, target, settings, production_id, reservation_id
            )
        if target.provider == "gemini" and settings.gemini_api_key:
            return _gemini_generate(
                prompt, target, settings, production_id, reservation_id
            )
        raise ScriptProviderUnavailable(
            f"{target.provider} is not configured for short scripting"
        )

    try:
        try:
            return generate_for(route.primary)
        except Exception as exc:
            if not safe_to_fail_over(exc) or route.fallback is None:
                raise
            return generate_for(route.fallback)
    except Exception as exc:
        release_budget_reservation(
            reservation_id,
            reason=f"short_script_failed:{type(exc).__name__}",
        )
        raise
