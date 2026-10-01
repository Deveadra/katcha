from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from katcha.ai.pricing import estimate_token_cost
from katcha.ai.provider_policy import command_provider_order
from katcha.ai.router import (
    ModelTarget,
    record_usage,
    release_budget_reservation,
    route_for_channel,
)
from katcha.config import Settings, get_settings
from katcha.domain import AITask
from katcha.integrations.chatgpt import invoke_json as invoke_chatgpt_json
from katcha.integrations.codex import invoke_json as invoke_codex_json

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
    detail = " ".join(str(exc).split())[:180]
    if detail:
        detail = re.sub(r"(?i)Bearer\\s+\\S+", "Bearer [REDACTED]", detail)
        detail = re.sub(r"\\bsk-[A-Za-z0-9_-]{8,}\\b", "[REDACTED]", detail)
        detail = re.sub(r"\\bAIza[A-Za-z0-9_-]{12,}\\b", "[REDACTED]", detail)
        suffix += f": {detail}"
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
        "Answer only from the supplied Katcha evidence and deterministic grounded "
        "summary. The grounded summary is server-produced state and may say that Katcha "
        "prepared a frozen action. Treat that as authoritative: a prepared action is not an "
        "executed action, but do not claim Katcha is unable to take a step the grounded "
        "summary says is ready. Never imply that an action ran unless the evidence or "
        "grounded summary explicitly says it ran. Do not invent clips, metrics, failures, "
        "causes, or YouTube results. If evidence is incomplete, say what is missing. "
        "Keep the answer "
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
    result = invoke_chatgpt_json(
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


def _codex(
    prompt: str,
    *,
    request_id: uuid.UUID,
) -> CommandNarrativeResult:
    result = invoke_codex_json(
        prompt=prompt,
        schema_name="katcha_command_narrative",
        schema=CommandNarrative.model_json_schema(),
    )
    target = ModelTarget("codex", result.model)
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

    client = genai.Client(api_key=settings.gemini_api_key)
    try:
        response = client.models.generate_content(
            model=target.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CommandNarrative,
            ),
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
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
        last_error: Exception | None = None
        for provider in command_provider_order(settings, intent):
            try:
                if provider == "gemini":
                    if not settings.gemini_api_key:
                        continue
                    target = ModelTarget("gemini", "gemini-3.5-flash-lite")
                    decision = route_for_channel(
                        AITask.COMMAND_PLANNING,
                        channel_profile_id,
                        estimated_increment_usd=Decimal("0.003"),
                        expected_value=0.55,
                        reference_type="command_center",
                        reference_id=str(request_id),
                        reservation_key=f"command-center:{request_id}:gemini",
                        preferred_target=target,
                    )
                    reservation_id = decision.reservation_id
                    return _gemini(
                        prompt,
                        target=target,
                        settings=settings,
                        request_id=request_id,
                        reservation_id=reservation_id,
                    )

                if provider == "codex":
                    if not getattr(settings, "codex_enabled", False):
                        continue
                    return _codex(prompt, request_id=request_id)

                if provider == "chatgpt":
                    if not getattr(settings, "chatgpt_host_id", None):
                        continue
                    return _chatgpt(prompt, request_id=request_id)

                if provider == "openai":
                    if not (
                        settings.openai_api_key
                        and getattr(settings, "allow_paid_openai_fallback", False)
                    ):
                        continue
                    target = ModelTarget("openai", "gpt-5.6-luna")
                    decision = route_for_channel(
                        AITask.COMMAND_PLANNING,
                        channel_profile_id,
                        estimated_increment_usd=Decimal("0.01"),
                        expected_value=0.7,
                        reference_type="command_center",
                        reference_id=str(request_id),
                        reservation_key=f"command-center:{request_id}:openai",
                        preferred_target=target,
                    )
                    reservation_id = decision.reservation_id
                    return _openai(
                        prompt,
                        target=target,
                        settings=settings,
                        request_id=request_id,
                        reservation_id=reservation_id,
                    )
            except Exception as exc:
                last_error = exc
                model = {
                    "gemini": "gemini-3.5-flash-lite",
                    "codex": "plan",
                    "chatgpt": "plan",
                    "openai": "gpt-5.6-luna",
                }.get(provider, "unknown")
                attempts.append(_attempt_label(ModelTarget(provider, model), exc))
                release_budget_reservation(
                    reservation_id,
                    reason=f"command_center_provider_failed:{provider}:{type(exc).__name__}",
                )
                reservation_id = None
                continue

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
