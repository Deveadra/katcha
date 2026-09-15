from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import CompilationStatus, ReviewDecision
from katcha.editorial.personas import get_persona
from katcha.longform_models import (
    Compilation,
    CompilationAsset,
    CompilationReview,
    CompilationSegment,
)
from katcha.models import DomainEvent

PROMPT_VERSION = "longform-v1"
REGENERATE_STAGES = {"plan", "voice", "render"}


def _workflow_id(idempotency_key: str | None) -> str:
    if idempotency_key:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        return f"longform-{digest}"
    return f"longform-{uuid.uuid4().hex[:24]}"


def register_compilation(
    *,
    theme: str,
    target_duration_seconds: int | None = None,
    target_segment_count: int | None = None,
    persona_key: str = "youth_host",
    idempotency_key: str | None = None,
) -> Compilation:
    settings = get_settings()
    persona = get_persona(persona_key)
    theme = theme.strip()
    if not theme:
        raise ValueError("compilation theme cannot be empty")
    if len(theme) > 500:
        raise ValueError("compilation theme cannot exceed 500 characters")

    target_duration = target_duration_seconds or settings.longform_default_target_seconds
    if target_duration < 180 or target_duration > settings.longform_max_target_seconds:
        raise ValueError(
            "target duration must be between 180 and "
            f"{settings.longform_max_target_seconds} seconds"
        )
    if target_segment_count is not None and not (
        settings.longform_min_segments
        <= target_segment_count
        <= settings.longform_max_segments
    ):
        raise ValueError(
            "target segment count must be between "
            f"{settings.longform_min_segments} and {settings.longform_max_segments}"
        )

    workflow_id = _workflow_id(idempotency_key)
    with session_scope() as session:
        existing = session.scalar(
            select(Compilation).where(Compilation.workflow_id == workflow_id)
        )
        if existing is not None:
            session.expunge(existing)
            return existing

        compilation = Compilation(
            workflow_id=workflow_id,
            status=CompilationStatus.QUEUED.value,
            stage="queued",
            theme=theme,
            target_duration_seconds=target_duration,
            target_segment_count=target_segment_count,
            persona_key=persona.key,
            persona_version=persona.version,
            prompt_version=PROMPT_VERSION,
            estimated_cost_usd=Decimal("0"),
        )
        session.add(compilation)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=str(compilation.id),
                event_type="compilation.created",
                payload={
                    "compilation_id": str(compilation.id),
                    "workflow_id": workflow_id,
                    "theme": theme,
                    "target_duration_seconds": target_duration,
                    "target_segment_count": target_segment_count,
                },
            )
        )
        session.refresh(compilation)
        session.expunge(compilation)
        return compilation


def _clone_segments(session: object, parent: Compilation, child: Compilation) -> None:
    segments = list(
        session.scalars(
            select(CompilationSegment)
            .where(CompilationSegment.compilation_id == parent.id)
            .order_by(CompilationSegment.position)
        )
    )
    if not segments:
        raise ValueError("parent compilation has no finalized segments to reuse")
    for source in segments:
        session.add(
            CompilationSegment(
                compilation_id=child.id,
                position=source.position,
                clip_id=source.clip_id,
                short_production_id=source.short_production_id,
                short_publication_id=source.short_publication_id,
                deterministic_score=source.deterministic_score,
                opening_score=source.opening_score,
                source_duration_seconds=source.source_duration_seconds,
                source_start_seconds=source.source_start_seconds,
                source_end_seconds=source.source_end_seconds,
                transition_before=source.transition_before,
                host_before=source.host_before,
                host_after=source.host_after,
                selection_reason=source.selection_reason,
                evidence=dict(source.evidence or {}),
                timing={},
            )
        )


def _clone_narration_assets(session: object, parent: Compilation, child: Compilation) -> None:
    assets = list(
        session.scalars(
            select(CompilationAsset).where(CompilationAsset.compilation_id == parent.id)
        )
    )
    narration = [asset for asset in assets if asset.kind.startswith("narration_")]
    if not narration:
        raise ValueError("parent compilation has no narration assets to reuse")
    for source in narration:
        session.add(
            CompilationAsset(
                compilation_id=child.id,
                kind=source.kind,
                generation=1,
                storage_key=source.storage_key,
                content_type=source.content_type,
                provider=source.provider,
                model=source.model,
                asset_metadata={
                    **dict(source.asset_metadata or {}),
                    "reused_from": str(parent.id),
                },
            )
        )
    child.selected_voice_profile = parent.selected_voice_profile


def register_compilation_regeneration(
    compilation_id: uuid.UUID,
    *,
    stage: str,
    note: str | None = None,
    actor: str = "operator",
) -> Compilation:
    if stage not in REGENERATE_STAGES:
        raise ValueError(f"regenerate stage must be one of: {sorted(REGENERATE_STAGES)}")

    with session_scope() as session:
        parent = session.get(Compilation, compilation_id)
        if parent is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if parent.status not in {
            CompilationStatus.REVIEW.value,
            CompilationStatus.REJECTED.value,
            CompilationStatus.FAILED.value,
        }:
            raise ValueError(
                "compilation must be in review, rejected, or failed state to regenerate"
            )

        effective_stage = stage
        if stage == "plan" and not parent.candidate_snapshot:
            effective_stage = "select"

        child = Compilation(
            parent_compilation_id=parent.id,
            generation=parent.generation + 1,
            regenerate_from=effective_stage,
            workflow_id=_workflow_id(None),
            status=CompilationStatus.QUEUED.value,
            stage=f"regenerate_{effective_stage}_queued",
            theme=parent.theme,
            target_duration_seconds=parent.target_duration_seconds,
            target_segment_count=parent.target_segment_count,
            persona_key=parent.persona_key,
            persona_version=parent.persona_version,
            prompt_version=parent.prompt_version,
            candidate_snapshot=dict(parent.candidate_snapshot or {}),
            estimated_cost_usd=Decimal("0"),
        )
        session.add(child)
        session.flush()

        if stage in {"voice", "render"}:
            child.editor_plan = dict(parent.editor_plan or {})
            child.critic_feedback = dict(parent.critic_feedback or {})
            child.final_plan = dict(parent.final_plan or {})
            _clone_segments(session, parent, child)
            child.status = CompilationStatus.SCRIPTED.value
            child.stage = "plan_finalized"
        if stage == "render":
            _clone_narration_assets(session, parent, child)
            child.status = CompilationStatus.VOICED.value
            child.stage = "voice_ready"

        parent.status = CompilationStatus.REJECTED.value
        parent.stage = f"regenerated_from_{stage}"
        session.add(
            CompilationReview(
                compilation_id=parent.id,
                decision=ReviewDecision.REGENERATE.value,
                actor=actor,
                note=note,
                review_metadata={
                    "requested_stage": stage,
                    "effective_stage": effective_stage,
                    "child_compilation_id": str(child.id),
                },
            )
        )
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=str(parent.id),
                event_type="compilation.regenerated",
                payload={
                    "compilation_id": str(parent.id),
                    "child_compilation_id": str(child.id),
                    "regenerate_from": effective_stage,
                },
            )
        )
        session.refresh(child)
        session.expunge(child)
        return child


def review_compilation(
    compilation_id: uuid.UUID,
    *,
    decision: ReviewDecision,
    note: str | None = None,
    actor: str = "operator",
) -> CompilationReview:
    if decision == ReviewDecision.REGENERATE:
        raise ValueError("use register_compilation_regeneration for regenerate decisions")

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_id)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if compilation.status != CompilationStatus.REVIEW.value:
            raise ValueError("compilation is not awaiting review")

        review = CompilationReview(
            compilation_id=compilation.id,
            decision=decision.value,
            actor=actor,
            note=note,
            review_metadata={},
        )
        session.add(review)
        if decision == ReviewDecision.APPROVE:
            compilation.status = CompilationStatus.APPROVED.value
            compilation.stage = "approved"
            event_type = "compilation.approved"
        else:
            compilation.status = CompilationStatus.REJECTED.value
            compilation.stage = "rejected"
            event_type = "compilation.rejected"
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=str(compilation.id),
                event_type=event_type,
                payload={
                    "compilation_id": str(compilation.id),
                    "decision": decision.value,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(review)
        session.expunge(review)
        return review
