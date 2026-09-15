from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    assert_ai_budget,
    record_usage,
    route_for,
    route_for_channel,
)
from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.domain import AITask
from katcha.editorial.personas import HostPersona
from katcha.longform.schemas import (
    CandidateEvidence,
    FinalLongformPlan,
    LongformCritique,
    LongformEditorPlan,
)
from katcha.longform_models import Compilation


class LongformProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LongformAIResult:
    value: LongformEditorPlan | LongformCritique | FinalLongformPlan
    target: ModelTarget
    input_tokens: int
    output_tokens: int


def _candidate_payload(candidates: list[CandidateEvidence]) -> str:
    return json.dumps(
        [candidate.model_dump(mode="json") for candidate in candidates],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _routing_context(
    compilation_id: str,
    candidates: list[CandidateEvidence],
) -> tuple[uuid.UUID | None, float]:
    with session_scope() as session:
        compilation = session.get(Compilation, uuid.UUID(compilation_id))
        channel_profile_id = compilation.channel_profile_id if compilation else None
    expected = (
        sum(candidate.deterministic_score for candidate in candidates) / len(candidates)
        if candidates
        else 0.5
    )
    return channel_profile_id, max(0.0, min(1.0, expected))


def _record(
    *,
    task: AITask,
    target: ModelTarget,
    input_tokens: int,
    output_tokens: int,
    compilation_id: str,
    stage: str,
) -> None:
    record_usage(
        task=task,
        target=target,
        input_units=input_tokens,
        output_units=output_tokens,
        cost_usd=estimate_token_cost(target, input_tokens, output_tokens),
        reference_type="compilation",
        reference_id=compilation_id,
        metadata={
            "stage": stage,
            "estimated_cost": True,
            "pricing_basis": "public_paid_rate_2026-09-15",
        },
    )


def _editor_prompt(
    *,
    theme: str,
    target_duration_seconds: int,
    persona: HostPersona,
    candidates: list[CandidateEvidence],
    prompt_version: str,
) -> str:
    return (
        f"Prompt version: {prompt_version}\n"
        "You are the senior editor of a fast-moving YouTube compilation show. The candidate "
        "order supplied below was produced by Katcha's deterministic performance sequencer. "
        "Respect its evidence unless there is a clear pacing reason to change it. Never invent "
        "events that are not present in the candidate summaries.\n\n"
        f"Theme: {theme}\n"
        f"Target runtime before host padding: approximately {target_duration_seconds} seconds\n"
        f"Host audience: {persona.audience}\n"
        f"Host identity: {persona.identity}\n"
        f"Host delivery: {persona.delivery}\n"
        f"Comedy tools: {', '.join(persona.comedy)}\n"
        f"Avoid: {', '.join(persona.avoid)}\n\n"
        "Build a coherent long-form episode. The host should add framing, callbacks, judgments, "
        "or jokes rather than narrating obvious action. Keep host lines short enough that clips "
        "remain the main attraction. Use the strongest clip immediately; do not waste the first "
        "30 seconds on a long greeting. Vary rhythm and avoid several near-identical clip types "
        "back-to-back. Source ranges must stay inside the source duration. You may trim dead air "
        "but should preserve each payoff. Every planned clip_id must come from the candidates.\n\n"
        f"Candidates in deterministic starting order:\n{_candidate_payload(candidates)}"
    )


def _critic_prompt(
    *,
    theme: str,
    candidates: list[CandidateEvidence],
    plan: LongformEditorPlan,
) -> str:
    return (
        "Act as a skeptical retention editor reviewing a proposed YouTube compilation. "
        "Look for a weak cold open, repetitive adjacent segments, commentary that merely states "
        "what is visible, missing payoffs, source ranges that exceed the source clip, pacing "
        "dead zones, or an outro that drags. Be specific and conservative; do not invent facts. "
        "Return pass only when no medium/high issue materially warrants revision.\n\n"
        f"Theme: {theme}\n"
        f"Candidate evidence: {_candidate_payload(candidates)}\n"
        f"Proposed plan: {plan.model_dump_json()}"
    )


def _revision_prompt(
    *,
    theme: str,
    persona: HostPersona,
    candidates: list[CandidateEvidence],
    plan: LongformEditorPlan,
    critique: LongformCritique,
) -> str:
    return (
        "Revise this compilation plan as the senior editor. Apply useful critic feedback, but "
        "do not make changes merely to appear responsive. Keep every clip ID within the supplied "
        "candidate set, never repeat a clip, keep source ranges within source duration, preserve "
        "the strongest cold open, and keep host commentary additive rather than descriptive.\n\n"
        f"Theme: {theme}\n"
        f"Host: {persona.identity}; delivery: {persona.delivery}\n"
        f"Candidates: {_candidate_payload(candidates)}\n"
        f"Original plan: {plan.model_dump_json()}\n"
        f"Critique: {critique.model_dump_json()}"
    )


def _validate_plan(
    plan: LongformEditorPlan | FinalLongformPlan,
    candidates: list[CandidateEvidence],
    *,
    min_segments: int,
) -> None:
    if len(plan.segments) < min_segments:
        raise ValueError(
            f"editor plan contains {len(plan.segments)} segments; minimum is {min_segments}"
        )
    by_id = {candidate.clip_id: candidate for candidate in candidates}
    for segment in plan.segments:
        candidate = by_id.get(segment.clip_id)
        if candidate is None:
            raise ValueError(f"editor referenced non-candidate clip: {segment.clip_id}")
        end = segment.source_end_seconds
        if segment.source_start_seconds >= candidate.duration_seconds:
            raise ValueError(f"source start exceeds clip duration: {segment.clip_id}")
        if end is not None:
            if end <= segment.source_start_seconds:
                raise ValueError(f"source end must follow source start: {segment.clip_id}")
            if end > candidate.duration_seconds + 0.05:
                raise ValueError(f"source end exceeds clip duration: {segment.clip_id}")


def _openai_structured(
    *,
    prompt: str,
    schema: type[LongformEditorPlan] | type[LongformCritique] | type[FinalLongformPlan],
    target: ModelTarget,
    settings: Settings,
    task: AITask,
    compilation_id: str,
    stage: str,
    effort: str,
) -> LongformAIResult:
    if not settings.openai_api_key:
        raise LongformProviderUnavailable("OpenAI API key is not configured")
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=target.model,
        store=False,
        reasoning={"effort": effort},
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
                "strict": False,
            }
        },
        max_output_tokens=5000,
    )
    value = schema.model_validate_json(response.output_text)
    usage = response.usage
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    _record(
        task=task,
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        compilation_id=compilation_id,
        stage=stage,
    )
    return LongformAIResult(value, target, input_tokens, output_tokens)


def _gemini_structured(
    *,
    prompt: str,
    schema: type[LongformEditorPlan] | type[LongformCritique] | type[FinalLongformPlan],
    target: ModelTarget,
    settings: Settings,
    task: AITask,
    compilation_id: str,
    stage: str,
) -> LongformAIResult:
    if not settings.gemini_api_key:
        raise LongformProviderUnavailable("Gemini API key is not configured")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=target.model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    value = schema.model_validate_json(response.text)
    usage = response.usage_metadata
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    _record(
        task=task,
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        compilation_id=compilation_id,
        stage=stage,
    )
    return LongformAIResult(value, target, input_tokens, output_tokens)


def _run_structured(
    *,
    prompt: str,
    schema: type[LongformEditorPlan] | type[LongformCritique] | type[FinalLongformPlan],
    task: AITask,
    compilation_id: str,
    stage: str,
    candidates: list[CandidateEvidence],
    estimated_increment: Decimal,
    effort: str = "medium",
    settings: Settings,
) -> LongformAIResult:
    channel_profile_id, expected_value = _routing_context(compilation_id, candidates)
    if channel_profile_id is not None:
        route = route_for_channel(
            task,
            channel_profile_id,
            estimated_increment_usd=estimated_increment,
            expected_value=expected_value,
        ).route
    else:
        route = route_for(task)
    primary = route.primary
    if primary.provider == "openai" and settings.openai_api_key:
        return _openai_structured(
            prompt=prompt,
            schema=schema,
            target=primary,
            settings=settings,
            task=task,
            compilation_id=compilation_id,
            stage=stage,
            effort=effort,
        )
    if primary.provider == "gemini" and settings.gemini_api_key:
        return _gemini_structured(
            prompt=prompt,
            schema=schema,
            target=primary,
            settings=settings,
            task=task,
            compilation_id=compilation_id,
            stage=stage,
        )
    fallback = route.fallback
    if fallback and fallback.provider == "openai" and settings.openai_api_key:
        return _openai_structured(
            prompt=prompt,
            schema=schema,
            target=fallback,
            settings=settings,
            task=task,
            compilation_id=compilation_id,
            stage=stage,
            effort=effort,
        )
    if fallback and fallback.provider == "gemini" and settings.gemini_api_key:
        return _gemini_structured(
            prompt=prompt,
            schema=schema,
            target=fallback,
            settings=settings,
            task=task,
            compilation_id=compilation_id,
            stage=stage,
        )
    raise LongformProviderUnavailable(f"no configured provider is available for {task.value}")


def generate_editor_plan(
    *,
    theme: str,
    target_duration_seconds: int,
    persona: HostPersona,
    candidates: list[CandidateEvidence],
    prompt_version: str,
    compilation_id: str,
    min_segments: int,
    settings: Settings | None = None,
) -> LongformAIResult:
    settings = settings or get_settings()
    estimated_increment = Decimal("0.25")
    assert_ai_budget(estimated_increment)
    result = _run_structured(
        prompt=_editor_prompt(
            theme=theme,
            target_duration_seconds=target_duration_seconds,
            persona=persona,
            candidates=candidates,
            prompt_version=prompt_version,
        ),
        schema=LongformEditorPlan,
        task=AITask.LONGFORM_EDITOR,
        compilation_id=compilation_id,
        stage="editor_plan",
        candidates=candidates,
        estimated_increment=estimated_increment,
        effort="high",
        settings=settings,
    )
    assert isinstance(result.value, LongformEditorPlan)
    _validate_plan(result.value, candidates, min_segments=min_segments)
    return result


def critique_editor_plan(
    *,
    theme: str,
    candidates: list[CandidateEvidence],
    plan: LongformEditorPlan,
    compilation_id: str,
    settings: Settings | None = None,
) -> LongformAIResult:
    settings = settings or get_settings()
    estimated_increment = Decimal("0.15")
    assert_ai_budget(estimated_increment)
    return _run_structured(
        prompt=_critic_prompt(theme=theme, candidates=candidates, plan=plan),
        schema=LongformCritique,
        task=AITask.LONGFORM_CRITIC,
        compilation_id=compilation_id,
        stage="critic",
        candidates=candidates,
        estimated_increment=estimated_increment,
        settings=settings,
    )


def finalize_editor_plan(
    *,
    theme: str,
    persona: HostPersona,
    candidates: list[CandidateEvidence],
    plan: LongformEditorPlan,
    critique: LongformCritique,
    compilation_id: str,
    min_segments: int,
    settings: Settings | None = None,
) -> LongformAIResult | FinalLongformPlan:
    if critique.verdict == "pass":
        final = FinalLongformPlan(
            **plan.model_dump(),
            revision_summary="Gemini critic passed the initial editor plan without revision.",
        )
        _validate_plan(final, candidates, min_segments=min_segments)
        return final

    settings = settings or get_settings()
    estimated_increment = Decimal("0.25")
    assert_ai_budget(estimated_increment)
    result = _run_structured(
        prompt=_revision_prompt(
            theme=theme,
            persona=persona,
            candidates=candidates,
            plan=plan,
            critique=critique,
        ),
        schema=FinalLongformPlan,
        task=AITask.LONGFORM_EDITOR,
        compilation_id=compilation_id,
        stage="editor_revision",
        candidates=candidates,
        estimated_increment=estimated_increment,
        effort="high",
        settings=settings,
    )
    assert isinstance(result.value, FinalLongformPlan)
    _validate_plan(result.value, candidates, min_segments=min_segments)
    return result
