from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from katcha.ai.failover import safe_to_fail_over
from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    record_usage,
    release_budget_reservation,
    route_for_channel,
)
from katcha.config import Settings, get_settings
from katcha.domain import AITask


class CommandNarrative(BaseModel):
    answer: str = Field(min_length=1, max_length=6000)
    key_points: list[str] = Field(default_factory=list, max_length=8)
    caveats: list[str] = Field(default_factory=list, max_length=6)


@dataclass(frozen=True, slots=True)
class CommandNarrativeResult:
    value: CommandNarrative
    target: ModelTarget
    input_tokens: int
    output_tokens: int


def _prompt(
    *,
    user_prompt: str,
    intent: str,
    deterministic_answer: str,
    evidence: list[dict[str, object]],
) -> str:
    payload = json.dumps(evidence, ensure_ascii=False, default=str)
    return (
        "You are Katcha AI, the operator-facing intelligence interface for Katcha. "
        "Answer only from the supplied Katcha evidence. Never imply that an action ran unless "
        "the evidence explicitly says it ran. Do not invent clips, metrics, failures, causes, "
        "or YouTube results. If evidence is incomplete, say what is missing. Keep the answer "
        "direct and operational, then explain the strongest evidence.\n\n"
        f"Intent: {intent}\n"
        f"Operator request: {user_prompt}\n"
        f"Deterministic grounded summary: {deterministic_answer}\n"
        f"KATCHA_EVIDENCE_JSON: {payload}"
    )


def _record(
    *,
    target: ModelTarget,
    input_tokens: int,
    output_tokens: int,
    request_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
) -> None:
    record_usage(
        task=AITask.PERFORMANCE_ANALYSIS,
        target=target,
        input_units=input_tokens,
        output_units=output_tokens,
        cost_usd=estimate_token_cost(target, input_tokens, output_tokens),
        reference_type="command_center",
        reference_id=str(request_id),
        metadata={
            "surface": "katcha_ai_command_center",
            "grounded": True,
            "estimated_cost": True,
        },
        reservation_id=reservation_id,
    )


def _openai(
    prompt: str,
    *,
    target: ModelTarget,
    settings: Settings,
    request_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
) -> CommandNarrativeResult:
    from openai import OpenAI

    response = OpenAI(api_key=settings.openai_api_key).responses.create(
        model=target.model,
        store=False,
        reasoning={"effort": "low"},
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "katcha_command_narrative",
                "schema": CommandNarrative.model_json_schema(),
                "strict": False,
            }
        },
        max_output_tokens=1200,
    )
    usage = response.usage
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    _record(
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        request_id=request_id,
        reservation_id=reservation_id,
    )
    return CommandNarrativeResult(
        CommandNarrative.model_validate_json(response.output_text),
        target,
        input_tokens,
        output_tokens,
    )


def _gemini(
    prompt: str,
    *,
    target: ModelTarget,
    settings: Settings,
    request_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
) -> CommandNarrativeResult:
    from google import genai
    from google.genai import types

    response = genai.Client(api_key=settings.gemini_api_key).models.generate_content(
        model=target.model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CommandNarrative,
        ),
    )
    usage = response.usage_metadata
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    _record(
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        request_id=request_id,
        reservation_id=reservation_id,
    )
    return CommandNarrativeResult(
        CommandNarrative.model_validate_json(response.text),
        target,
        input_tokens,
        output_tokens,
    )


def compose_grounded_answer(
    *,
    channel_profile_id: uuid.UUID,
    request_id: uuid.UUID,
    user_prompt: str,
    intent: str,
    deterministic_answer: str,
    evidence: list[dict[str, object]],
    settings: Settings | None = None,
) -> CommandNarrativeResult:
    settings = settings or get_settings()
    fallback = CommandNarrativeResult(
        CommandNarrative(answer=deterministic_answer),
        ModelTarget("katcha", "grounded-deterministic-v1"),
        0,
        0,
    )
    if settings.resolved_ai_execution_mode() == "fixture":
        return fallback
    if not settings.ai_enabled:
        return fallback

    reservation_id: uuid.UUID | None = None
    try:
        decision = route_for_channel(
            AITask.PERFORMANCE_ANALYSIS,
            channel_profile_id,
            estimated_increment_usd=Decimal("0.03"),
            expected_value=0.7,
            reference_type="command_center",
            reference_id=str(request_id),
            reservation_key=f"command-center:{request_id}",
        )
        reservation_id = decision.reservation_id
        prompt = _prompt(
            user_prompt=user_prompt,
            intent=intent,
            deterministic_answer=deterministic_answer,
            evidence=evidence,
        )
        targets = [decision.route.primary]
        if decision.route.fallback is not None:
            targets.append(decision.route.fallback)
        last_error: Exception | None = None
        for index, target in enumerate(targets):
            try:
                if target.provider == "openai" and settings.openai_api_key:
                    return _openai(
                        prompt,
                        target=target,
                        settings=settings,
                        request_id=request_id,
                        reservation_id=reservation_id,
                    )
                if target.provider == "gemini" and settings.gemini_api_key:
                    return _gemini(
                        prompt,
                        target=target,
                        settings=settings,
                        request_id=request_id,
                        reservation_id=reservation_id,
                    )
            except Exception as exc:
                last_error = exc
                if index == 0 and len(targets) > 1 and safe_to_fail_over(exc):
                    continue
                break
        if last_error is not None:
            raise last_error
        raise RuntimeError("no configured provider is available for command-center narration")
    except Exception as exc:
        release_budget_reservation(
            reservation_id,
            reason=f"command_center_fallback:{type(exc).__name__}",
        )
        return fallback
