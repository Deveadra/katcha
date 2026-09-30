from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from katcha.ai.failover import safe_to_fail_over_generation
from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    record_usage,
    release_budget_reservation,
    route_for_channel,
)
from katcha.config import Settings, get_settings
from katcha.domain import AITask
from katcha.integrations.chatgpt import ChatGPTConnectionError, invoke_json

logger = logging.getLogger(__name__)


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
    degraded_reason: str | None = None


def _failure_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _attempt_label(target: ModelTarget, exc: BaseException) -> str:
    status = _failure_status(exc)
    suffix = f"HTTP {status}" if status is not None else type(exc).__name__
    return f"{target.provider}/{target.model} ({suffix})"


def _degraded_notice(exc: BaseException, attempts: list[str]) -> str:
    if attempts:
        return (
            "Live AI could not complete this answer after trying "
            + " → ".join(attempts)
            + ". Katcha used saved data instead. Check provider credentials, "
            "model access, or provider quota in the launch console."
        )
    name = type(exc).__name__
    if name == "ChannelBudgetExceeded":
        return (
            "Live AI is blocked by this channel's AI budget headroom. "
            "Katcha used saved data instead."
        )
    if name == "BudgetExceeded":
        return (
            "No eligible live AI provider is available for this channel. "
            "Katcha used saved data instead; check provider configuration and budget."
        )
    return (
        f"Live AI routing failed ({name}). Katcha used saved data instead. "
        "Check AI configuration and the launcher diagnostics."
    )


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
        "direct and operational, then explain the strongest evidence. For conversation, "
        "respond naturally to greetings and capability questions. Strategy suggestions "
        "must be labeled as suggestions; never portray them as measured results.\n\n"
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
        task=AITask.COMMAND_PLANNING,
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

    response = OpenAI(
        api_key=settings.openai_api_key, timeout=30.0, max_retries=1
    ).responses.create(
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
        max_output_tokens=2400,
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


def _chatgpt(
    prompt: str,
    *,
    request_id: uuid.UUID,
) -> CommandNarrativeResult:
    result = invoke_json(
        prompt=prompt,
        schema_name="katcha_command_narrative",
        schema=CommandNarrative.model_json_schema(),
    )
    target = ModelTarget("chatgpt", result.model)
    _record(
        target=target,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        request_id=request_id,
        reservation_id=None,
    )
    return CommandNarrativeResult(
        CommandNarrative.model_validate_json(result.text),
        target,
        result.input_tokens,
        result.output_tokens,
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
        "Live AI is not configured for this workspace.",
    )
    if settings.resolved_ai_execution_mode() == "fixture":
        return CommandNarrativeResult(
            fallback.value,
            fallback.target,
            0,
            0,
            "Katcha is in fixture mode; this answer uses saved data only.",
        )
    if not settings.ai_enabled:
        return fallback

    reservation_id: uuid.UUID | None = None
    attempts: list[str] = []
    prompt = _prompt(
        user_prompt=user_prompt,
        intent=intent,
        deterministic_answer=deterministic_answer,
        evidence=evidence,
    )

    try:
        if getattr(settings, "chatgpt_host_id", None):
            try:
                return _chatgpt(prompt, request_id=request_id)
            except Exception as exc:
                if not (
                    isinstance(exc, ChatGPTConnectionError)
                    and "No ChatGPT plan connection is available" in str(exc)
                ):
                    attempts.append(f"chatgpt/plan ({type(exc).__name__})")

        decision = route_for_channel(
            AITask.COMMAND_PLANNING,
            channel_profile_id,
            estimated_increment_usd=Decimal("0.01"),
            expected_value=0.7,
            reference_type="command_center",
            reference_id=str(request_id),
            reservation_key=f"command-center:{request_id}",
        )
        reservation_id = decision.reservation_id
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
                attempts.append(_attempt_label(target, exc))
                if index < len(targets) - 1 and safe_to_fail_over_generation(exc):
                    continue
                break
        if last_error is not None:
            raise last_error
        raise RuntimeError("no configured provider is available for command-center narration")
    except Exception as exc:
        logger.warning(
            "Katcha AI answer unavailable request_id=%s cause=%s attempts=%s",
            request_id,
            type(exc).__name__,
            attempts,
        )
        release_budget_reservation(
            reservation_id,
            reason=f"command_center_fallback:{type(exc).__name__}",
        )
        return CommandNarrativeResult(
            fallback.value,
            fallback.target,
            0,
            0,
            _degraded_notice(
                exc,
                attempts,
            ),
        )
