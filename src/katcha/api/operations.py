from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from katcha.api.control_auth import control_allowed_channel_ids, require_control_channel
from katcha.db import session_scope
from katcha.acquisition_models import DiscoveryCandidate, IntelligenceRecord
from katcha.intelligence_models import ChannelProfile
from katcha.longform_models import Compilation, CompilationAsset
from katcha.models import DomainEvent, SourceItem
from katcha.orchestration.client import (
    start_longform_workflow,
    start_production_workflow,
    start_publication_workflow,
    start_short_episode_editorial_workflow,
)
from katcha.production_models import Production, ProductionAsset
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.render_models import RenderAttempt
from katcha.services.compilations import register_compilation_regeneration
from katcha.services.productions import register_regeneration
from katcha.services.publications import retry_publication
from katcha.services.short_episode_reviews import register_short_episode_regeneration
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset
from katcha.trend_models import (
    ChannelTrendWatchVersion,
    TrendOpportunity,
    TrendTopic,
)

router = APIRouter(prefix="/v1/operations", tags=["operations"])


@router.get("/system-check")
async def check_systems(request: Request) -> dict[str, object]:
    from katcha.api.control_auth import require_control_scope
    from katcha.services.system_check import system_check

    require_control_scope(request, "ai:read")
    return await system_check()

WorkState = Literal["attention", "active"]


class OperationsSummary(BaseModel):
    active_channels: int
    active_work: int
    needs_attention: int
    fresh_opportunities: int
    published_last_7d: int


class OperationsChannelSummary(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    timezone: str
    active_work: int
    needs_attention: int
    fresh_opportunities: int
    published_last_7d: int
    href: str


class OperationsIntakeItem(BaseModel):
    id: uuid.UUID
    channel_profile_id: uuid.UUID
    title: str
    status: str
    stage: str
    message: str | None = None
    source_url: str | None = None
    updated_at: datetime
    href: str


class OperationsWorkItem(BaseModel):
    kind: Literal["short_episode", "production", "compilation", "publication"]
    id: uuid.UUID
    channel_profile_id: uuid.UUID
    title: str
    status: str
    stage: str
    state: WorkState
    message: str | None = None
    updated_at: datetime
    href: str
    recovery_action: Literal["resume", "restart"] | None = None
    recovery_label: str | None = None


class RecoverWorkRequest(BaseModel):
    actor: str = Field(default="operator", min_length=1, max_length=128)
    allow_new_upload_session: bool = False


class RecoverWorkResponse(BaseModel):
    kind: Literal["short_episode", "production", "compilation", "publication"]
    source_id: uuid.UUID
    action: Literal["resumed", "restarted"]
    workflow_id: str
    replacement_id: uuid.UUID | None = None


class OperationsOpportunity(BaseModel):
    id: uuid.UUID
    channel_profile_id: uuid.UUID
    topic: str
    lifecycle: str
    opportunity_score: Decimal
    confidence: Decimal
    expires_at: datetime
    reasons: list[str] = Field(default_factory=list)
    href: str = "/explorer"


class OperationsPublication(BaseModel):
    id: uuid.UUID
    channel_profile_id: uuid.UUID
    title: str
    status: str
    youtube_video_id: str | None = None
    published_at: datetime | None = None
    sampled_at: datetime | None = None
    views: int | None = None
    average_view_percentage: Decimal | None = None
    estimated_revenue: Decimal | None = None
    href: str


class OperationsActivity(BaseModel):
    event_type: str
    aggregate_type: str
    aggregate_id: str
    channel_profile_id: uuid.UUID | None = None
    created_at: datetime
    href: str | None = None


class OperationsOverviewResponse(BaseModel):
    generated_at: datetime
    summary: OperationsSummary
    channels: list[OperationsChannelSummary]
    intake: list[OperationsIntakeItem]
    attention: list[OperationsWorkItem]
    active: list[OperationsWorkItem]
    opportunities: list[OperationsOpportunity]
    publications: list[OperationsPublication]
    activity: list[OperationsActivity]


_DONE_WORK_STATUSES = {"published", "completed", "rejected", "cancelled"}
_ATTENTION_STATUS_MARKERS = ("fail", "error", "dead_letter", "blocked")
_DONE_PUBLICATION_STATUSES = {"published", "private", "unlisted"}


def _channel_title(profile: ChannelProfile) -> str:
    metadata = dict(profile.profile_metadata or {})
    return str(
        metadata.get("channel_title")
        or metadata.get("name")
        or metadata.get("channel_handle")
        or profile.id
    )


def _work_state(
    *,
    status: str | None,
    error: str | None,
    render_status: str | None,
) -> WorkState | None:
    normalized = str(status or "").lower()
    if (
        render_status == "dead_letter"
        or bool((error or "").strip())
        or any(marker in normalized for marker in _ATTENTION_STATUS_MARKERS)
    ):
        return "attention"
    if normalized in _DONE_WORK_STATUSES:
        return None
    return "active"


def _item_href(channel_profile_id: uuid.UUID) -> str:
    return f"/editing?channel={channel_profile_id}#editorial-pipeline"


def _publication_href(channel_profile_id: uuid.UUID) -> str:
    return f"/channels?channel={channel_profile_id}#content"


def _production_resume_stage(row: Production) -> str | None:
    status = str(row.status or "").lower()
    if status in {"queued", "scripting"}:
        return "script"
    if status in {"scripted", "voicing"}:
        return "voice"
    if status in {"voiced", "rendering"}:
        return "render"
    return None


def _compilation_resume_stage(row: Compilation) -> str | None:
    status = str(row.status or "").lower()
    if status in {"queued", "selecting"}:
        return "select"
    if status in {"planning", "critiquing"}:
        return "plan"
    if status in {"scripted", "voicing"}:
        return "voice"
    if status in {"voiced", "rendering"}:
        return "render"
    return None


def _short_episode_resume_stage(row: ShortEpisode) -> str | None:
    status = str(row.status or "").lower()
    stage = str(row.stage or "").lower()
    if status == "planned" and stage in {"script_queued", "regenerate_script_queued"}:
        return "script"
    if status == "scripting":
        return "script"
    if status in {"scripted", "voicing"}:
        return "voice"
    if status == "planned" and stage == "regenerate_voice_queued":
        return "voice"
    if status in {"editorial_approved", "rendering"}:
        return "render"
    if status == "planned" and stage == "render_regeneration_queued":
        return "render"
    return None


def _work_recovery(
    *,
    kind: str,
    status: str,
    stage: str,
    state: WorkState,
    row: Production | ShortEpisode | Compilation | Publication,
    render_dead_letter: bool = False,
) -> tuple[str | None, str | None]:
    del state  # Recovery is determined by durable execution state, not display severity.
    normalized_status = str(status or "").lower()
    normalized_stage = str(stage or "").lower()

    if kind == "publication":
        if normalized_status == "failed" or normalized_stage == "processing_timeout":
            return "restart", "Restart"
        if normalized_status in {"queued", "uploading", "uploaded", "processing"}:
            return "resume", "Resume"
        return None, None

    if normalized_status == "failed" or render_dead_letter:
        return "restart", "Restart"
    if kind == "production" and isinstance(row, Production):
        return ("resume", "Resume") if _production_resume_stage(row) else (None, None)
    if kind == "short_episode" and isinstance(row, ShortEpisode):
        return ("resume", "Resume") if _short_episode_resume_stage(row) else (None, None)
    if kind == "compilation" and isinstance(row, Compilation):
        return ("resume", "Resume") if _compilation_resume_stage(row) else (None, None)
    return None, None


def _production_restart_stage(production_id: uuid.UUID) -> str:
    with session_scope() as session:
        row = session.get(Production, production_id)
        if row is None:
            raise ValueError("production not found")
        if row.selected_script_id is None:
            return "script"
        narration = session.scalar(
            select(ProductionAsset.id)
            .where(
                ProductionAsset.production_id == production_id,
                ProductionAsset.kind.like("narration_%"),
            )
            .limit(1)
        )
        return "render" if narration is not None else "voice"


def _short_episode_restart_stage(episode_id: uuid.UUID) -> str:
    with session_scope() as session:
        row = session.get(ShortEpisode, episode_id)
        if row is None:
            raise ValueError("short episode not found")
        if row.selected_script_id is None:
            return "script"
        narration = session.scalar(
            select(ShortEpisodeAsset.id)
            .where(
                ShortEpisodeAsset.short_episode_id == episode_id,
                ShortEpisodeAsset.kind.like("narration_%"),
            )
            .limit(1)
        )
        return "render" if narration is not None else "voice"


def _compilation_restart_stage(compilation_id: uuid.UUID) -> str:
    with session_scope() as session:
        row = session.get(Compilation, compilation_id)
        if row is None:
            raise ValueError("compilation not found")
        if not row.final_plan:
            return "plan"
        narration = session.scalar(
            select(CompilationAsset.id)
            .where(
                CompilationAsset.compilation_id == compilation_id,
                CompilationAsset.kind.like("narration_%"),
            )
            .limit(1)
        )
        return "render" if narration is not None else "voice"


def _episode_workflow_id(base_workflow_id: str, start_stage: str) -> str:
    return f"{base_workflow_id}-editorial-{start_stage}"


def _latest_attempts(
    attempts: list[RenderAttempt],
) -> dict[tuple[str, uuid.UUID], RenderAttempt]:
    result: dict[tuple[str, uuid.UUID], RenderAttempt] = {}
    for attempt in attempts:
        if attempt.production_id is not None:
            key = ("production", attempt.production_id)
        elif attempt.short_episode_id is not None:
            key = ("short_episode", attempt.short_episode_id)
        else:
            continue
        current = result.get(key)
        if current is None or attempt.attempt_number > current.attempt_number:
            result[key] = attempt
    return result


def _event_channel_id(
    event: DomainEvent,
    *,
    production_channels: dict[uuid.UUID, uuid.UUID],
    episode_channels: dict[uuid.UUID, uuid.UUID],
    compilation_channels: dict[uuid.UUID, uuid.UUID],
    publication_channels: dict[uuid.UUID, uuid.UUID],
) -> uuid.UUID | None:
    payload = dict(event.payload or {})
    raw = payload.get("channel_profile_id")
    if raw:
        try:
            return uuid.UUID(str(raw))
        except ValueError:
            return None
    if event.aggregate_type == "channel_profile":
        try:
            return uuid.UUID(event.aggregate_id)
        except ValueError:
            return None
    try:
        aggregate_id = uuid.UUID(event.aggregate_id)
    except ValueError:
        return None
    if event.aggregate_type == "production":
        return production_channels.get(aggregate_id)
    if event.aggregate_type == "short_episode":
        return episode_channels.get(aggregate_id)
    if event.aggregate_type == "compilation":
        return compilation_channels.get(aggregate_id)
    if event.aggregate_type == "publication":
        return publication_channels.get(aggregate_id)
    return None


def _event_href(
    event: DomainEvent,
    channel_profile_id: uuid.UUID | None,
) -> str | None:
    if channel_profile_id is None:
        return None
    if event.aggregate_type in {"production", "short_episode", "compilation", "render_attempt"}:
        return _item_href(channel_profile_id)
    if event.aggregate_type == "publication":
        return _publication_href(channel_profile_id)
    if event.aggregate_type == "channel_profile":
        return f"/channels?channel={channel_profile_id}"
    return None


@router.post(
    "/work/{kind}/{source_id}/recover",
    response_model=RecoverWorkResponse,
)
async def recover_work_item(
    kind: Literal["short_episode", "production", "compilation", "publication"],
    source_id: uuid.UUID,
    http_request: Request,
    request: RecoverWorkRequest,
) -> RecoverWorkResponse:
    if kind == "production":
        with session_scope() as session:
            row = session.get(Production, source_id)
            if row is None or row.channel_profile_id is None:
                raise HTTPException(status_code=404, detail="production not found")
            require_control_channel(http_request, row.channel_profile_id)
            status_value = str(row.status or "").lower()
            workflow_id = row.workflow_id
            resume_stage = _production_resume_stage(row)
            latest_attempt = session.scalar(
                select(RenderAttempt)
                .where(RenderAttempt.production_id == source_id)
                .order_by(RenderAttempt.attempt_number.desc())
                .limit(1)
            )
            render_dead_letter = bool(
                latest_attempt is not None and latest_attempt.status == "dead_letter"
            )

        if status_value == "failed" or render_dead_letter:
            try:
                stage = _production_restart_stage(source_id)
                child = register_regeneration(
                    source_id,
                    stage=stage,
                    note="Restarted from Operations after interrupted or failed work",
                    actor=request.actor,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            await start_production_workflow(
                str(child.id),
                child.workflow_id,
                start_stage=stage,
            )
            return RecoverWorkResponse(
                kind=kind,
                source_id=source_id,
                action="restarted",
                workflow_id=child.workflow_id,
                replacement_id=child.id,
            )

        if resume_stage is None:
            raise HTTPException(
                status_code=409,
                detail="production is not in a resumable execution stage",
            )
        await start_production_workflow(
            str(source_id),
            workflow_id,
            start_stage=resume_stage,
            allow_failed_reuse=True,
        )
        return RecoverWorkResponse(
            kind=kind,
            source_id=source_id,
            action="resumed",
            workflow_id=workflow_id,
        )

    if kind == "short_episode":
        with session_scope() as session:
            row = session.get(ShortEpisode, source_id)
            if row is None:
                raise HTTPException(status_code=404, detail="short episode not found")
            require_control_channel(http_request, row.channel_profile_id)
            status_value = str(row.status or "").lower()
            base_workflow_id = row.workflow_id
            resume_stage = _short_episode_resume_stage(row)
            latest_attempt = session.scalar(
                select(RenderAttempt)
                .where(RenderAttempt.short_episode_id == source_id)
                .order_by(RenderAttempt.attempt_number.desc())
                .limit(1)
            )
            render_dead_letter = bool(
                latest_attempt is not None and latest_attempt.status == "dead_letter"
            )

        if status_value == "failed" or render_dead_letter:
            try:
                stage = _short_episode_restart_stage(source_id)
                child = register_short_episode_regeneration(
                    source_id,
                    stage=stage,
                    note="Restarted from Operations after interrupted or failed work",
                    actor=request.actor,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            child_workflow_id = _episode_workflow_id(child.workflow_id, stage)
            await start_short_episode_editorial_workflow(
                str(child.id),
                child_workflow_id,
                start_stage=stage,
            )
            return RecoverWorkResponse(
                kind=kind,
                source_id=source_id,
                action="restarted",
                workflow_id=child_workflow_id,
                replacement_id=child.id,
            )

        if resume_stage is None:
            raise HTTPException(
                status_code=409,
                detail="short episode is not in a resumable execution stage",
            )
        workflow_id = _episode_workflow_id(base_workflow_id, resume_stage)
        await start_short_episode_editorial_workflow(
            str(source_id),
            workflow_id,
            start_stage=resume_stage,
            allow_failed_reuse=True,
        )
        return RecoverWorkResponse(
            kind=kind,
            source_id=source_id,
            action="resumed",
            workflow_id=workflow_id,
        )

    if kind == "compilation":
        with session_scope() as session:
            row = session.get(Compilation, source_id)
            if row is None or row.channel_profile_id is None:
                raise HTTPException(status_code=404, detail="compilation not found")
            require_control_channel(http_request, row.channel_profile_id)
            status_value = str(row.status or "").lower()
            workflow_id = row.workflow_id
            resume_stage = _compilation_resume_stage(row)

        if status_value == "failed":
            try:
                stage = _compilation_restart_stage(source_id)
                child = register_compilation_regeneration(
                    source_id,
                    stage=stage,
                    note="Restarted from Operations after interrupted or failed work",
                    actor=request.actor,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            child_stage = str(child.regenerate_from or stage)
            await start_longform_workflow(
                str(child.id),
                child.workflow_id,
                start_stage=child_stage,
            )
            return RecoverWorkResponse(
                kind=kind,
                source_id=source_id,
                action="restarted",
                workflow_id=child.workflow_id,
                replacement_id=child.id,
            )

        if resume_stage is None:
            raise HTTPException(
                status_code=409,
                detail="compilation is not in a resumable execution stage",
            )
        await start_longform_workflow(
            str(source_id),
            workflow_id,
            start_stage=resume_stage,
            allow_failed_reuse=True,
        )
        return RecoverWorkResponse(
            kind=kind,
            source_id=source_id,
            action="resumed",
            workflow_id=workflow_id,
        )

    with session_scope() as session:
        row = session.get(Publication, source_id)
        if row is None:
            raise HTTPException(status_code=404, detail="publication not found")
        profile = session.scalar(
            select(ChannelProfile).where(
                ChannelProfile.youtube_connection_id == row.youtube_connection_id
            )
        )
        if profile is None:
            raise HTTPException(
                status_code=409,
                detail="publication is not attached to an active channel profile",
            )
        require_control_channel(http_request, profile.id)
        status_value = str(row.status or "").lower()
        stage_value = str(row.stage or "").lower()
        workflow_id = row.workflow_id

    if status_value == "failed" or stage_value == "processing_timeout":
        try:
            publication = retry_publication(
                source_id,
                allow_new_upload_session=request.allow_new_upload_session,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await start_publication_workflow(
            str(publication.id),
            publication.workflow_id,
        )
        return RecoverWorkResponse(
            kind=kind,
            source_id=source_id,
            action="restarted",
            workflow_id=publication.workflow_id,
        )

    if status_value not in {"queued", "uploading", "uploaded", "processing"}:
        raise HTTPException(
            status_code=409,
            detail="publication is not in a resumable execution stage",
        )
    await start_publication_workflow(
        str(source_id),
        workflow_id,
        allow_failed_reuse=True,
    )
    return RecoverWorkResponse(
        kind=kind,
        source_id=source_id,
        action="resumed",
        workflow_id=workflow_id,
    )


@router.get("/overview", response_model=OperationsOverviewResponse)
def operations_overview(
    http_request: Request,
    channel_profile_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=12, ge=3, le=50),
) -> OperationsOverviewResponse:
    now = datetime.now(UTC)
    allowed = control_allowed_channel_ids(http_request)
    if channel_profile_id is not None:
        require_control_channel(http_request, channel_profile_id)

    with session_scope() as session:
        profile_stmt = (
            select(ChannelProfile)
            .where(ChannelProfile.status == "active")
            .order_by(ChannelProfile.created_at.asc())
        )
        if allowed is not None:
            profile_stmt = profile_stmt.where(ChannelProfile.id.in_(allowed))
        if channel_profile_id is not None:
            profile_stmt = profile_stmt.where(ChannelProfile.id == channel_profile_id)
        profiles = list(session.scalars(profile_stmt))
        if channel_profile_id is not None and not profiles:
            raise HTTPException(status_code=404, detail="channel profile not found")

        channel_ids = [profile.id for profile in profiles]
        if not channel_ids:
            empty = OperationsSummary(
                active_channels=0,
                active_work=0,
                needs_attention=0,
                fresh_opportunities=0,
                published_last_7d=0,
            )
            return OperationsOverviewResponse(
                generated_at=now,
                summary=empty,
                channels=[],
                intake=[],
                attention=[],
                active=[],
                opportunities=[],
                publications=[],
                activity=[],
            )

        profile_by_id = {profile.id: profile for profile in profiles}

        intelligence_records = list(
            session.scalars(
                select(IntelligenceRecord)
                .where(IntelligenceRecord.channel_profile_id.in_(channel_ids))
                .order_by(IntelligenceRecord.updated_at.desc())
                .limit(max(limit * 8, 80))
            )
        )
        record_ids = {str(row.id) for row in intelligence_records}
        candidate_by_record: dict[str, DiscoveryCandidate] = {}
        source_by_record: dict[str, SourceItem] = {}
        if record_ids:
            candidate_rows = list(
                session.scalars(
                    select(DiscoveryCandidate)
                    .order_by(DiscoveryCandidate.updated_at.desc())
                    .limit(max(limit * 20, 200))
                )
            )
            for candidate in candidate_rows:
                metadata = dict(candidate.candidate_metadata or {})
                record_id = str(metadata.get("intelligence_record_id") or "")
                channel_value = str(metadata.get("channel_profile_id") or "")
                if (
                    record_id in record_ids
                    and channel_value in {str(value) for value in channel_ids}
                    and record_id not in candidate_by_record
                ):
                    candidate_by_record[record_id] = candidate
            source_rows = list(
                session.scalars(
                    select(SourceItem)
                    .order_by(SourceItem.updated_at.desc())
                    .limit(max(limit * 20, 200))
                )
            )
            for source in source_rows:
                metadata = dict(source.source_metadata or {})
                record_id = str(metadata.get("intelligence_record_id") or "")
                channel_value = str(metadata.get("channel_profile_id") or "")
                if (
                    record_id in record_ids
                    and channel_value in {str(value) for value in channel_ids}
                    and record_id not in source_by_record
                ):
                    source_by_record[record_id] = source

        intake: list[OperationsIntakeItem] = []
        for record in intelligence_records:
            record_id = str(record.id)
            candidate = candidate_by_record.get(record_id)
            source = source_by_record.get(record_id)
            payload = dict(record.payload or {})
            if source is not None:
                normalized = str(source.status or "").lower()
                if normalized == "registered":
                    stage = "queued_download"
                elif normalized == "ingesting":
                    stage = "downloading"
                elif normalized == "ready" and source.clip_id is not None:
                    stage = "downloaded"
                elif normalized == "failed":
                    stage = "download_failed"
                else:
                    stage = normalized or "acquisition"
                status_value = source.status
                message = source.error or (
                    "Source media acquired and ready for preparation."
                    if stage == "downloaded"
                    else "Source acquisition is moving through Katcha."
                )
                updated_at = source.updated_at
            elif candidate is not None:
                normalized = str(candidate.status or "").lower()
                stage = {
                    "discovered": "candidate_created",
                    "qualified": "qualified_for_acquisition",
                    "promoted": "queued_download",
                    "review": "needs_attention",
                    "rejected": "rejected",
                }.get(normalized, normalized or "candidate")
                status_value = candidate.status
                message = (
                    "Verified candidate is ready for acquisition."
                    if normalized == "qualified"
                    else "Discovery candidate created from retained intelligence."
                )
                updated_at = candidate.updated_at
            else:
                stage = "handoff_received"
                status_value = record.status
                message = (
                    "Handoff stored; waiting for automation to create the acquisition candidate."
                    if payload.get("production_intent")
                    else record.summary
                )
                updated_at = record.updated_at
            intake.append(
                OperationsIntakeItem(
                    id=record.id,
                    channel_profile_id=record.channel_profile_id,
                    title=record.title or record.record_key,
                    status=str(status_value or "active"),
                    stage=stage,
                    message=message,
                    source_url=record.source_url,
                    updated_at=updated_at,
                    href=f"/channels?channel={record.channel_profile_id}#content",
                )
            )
            if len(intake) >= limit:
                break

        youtube_to_channel = {
            profile.youtube_connection_id: profile.id for profile in profiles
        }

        episodes = list(
            session.scalars(
                select(ShortEpisode)
                .where(
                    ShortEpisode.channel_profile_id.in_(channel_ids),
                    ~ShortEpisode.status.in_(_DONE_WORK_STATUSES),
                )
                .order_by(ShortEpisode.updated_at.desc())
            )
        )
        productions = list(
            session.scalars(
                select(Production)
                .where(
                    Production.channel_profile_id.in_(channel_ids),
                    ~Production.status.in_(_DONE_WORK_STATUSES),
                )
                .order_by(Production.updated_at.desc())
            )
        )
        compilations = list(
            session.scalars(
                select(Compilation)
                .where(
                    Compilation.channel_profile_id.in_(channel_ids),
                    ~Compilation.status.in_(_DONE_WORK_STATUSES),
                )
                .order_by(Compilation.updated_at.desc())
            )
        )
        attempts = list(
            session.scalars(
                select(RenderAttempt)
                .where(RenderAttempt.channel_profile_id.in_(channel_ids))
                .order_by(RenderAttempt.updated_at.desc())
            )
        )
        latest_attempt = _latest_attempts(attempts)

        work_items: list[OperationsWorkItem] = []
        episode_channels = {row.id: row.channel_profile_id for row in episodes}
        production_channels = {
            row.id: row.channel_profile_id
            for row in productions
            if row.channel_profile_id is not None
        }
        compilation_channels = {
            row.id: row.channel_profile_id
            for row in compilations
            if row.channel_profile_id is not None
        }

        for row in episodes:
            attempt = latest_attempt.get(("short_episode", row.id))
            state = _work_state(
                status=row.status,
                error=row.error,
                render_status=attempt.status if attempt else None,
            )
            if state is None:
                continue
            message = (
                (attempt.error if attempt and attempt.status == "dead_letter" else None)
                or row.error
                or f"{row.stage} · {row.status}"
            )
            recovery_action, recovery_label = _work_recovery(
                kind="short_episode",
                status=row.status,
                stage=row.stage,
                state=state,
                row=row,
                render_dead_letter=bool(
                    attempt is not None and attempt.status == "dead_letter"
                ),
            )
            work_items.append(
                OperationsWorkItem(
                    kind="short_episode",
                    id=row.id,
                    channel_profile_id=row.channel_profile_id,
                    title=row.premise,
                    status=row.status,
                    stage=row.stage,
                    state=state,
                    message=message,
                    updated_at=row.updated_at,
                    href=_item_href(row.channel_profile_id),
                    recovery_action=recovery_action,
                    recovery_label=recovery_label,
                )
            )

        for row in productions:
            if row.channel_profile_id is None:
                continue
            attempt = latest_attempt.get(("production", row.id))
            state = _work_state(
                status=row.status,
                error=row.error,
                render_status=attempt.status if attempt else None,
            )
            if state is None:
                continue
            message = (
                (attempt.error if attempt and attempt.status == "dead_letter" else None)
                or row.error
                or f"{row.stage} · {row.status}"
            )
            recovery_action, recovery_label = _work_recovery(
                kind="production",
                status=row.status,
                stage=row.stage,
                state=state,
                row=row,
                render_dead_letter=bool(
                    attempt is not None and attempt.status == "dead_letter"
                ),
            )
            work_items.append(
                OperationsWorkItem(
                    kind="production",
                    id=row.id,
                    channel_profile_id=row.channel_profile_id,
                    title=f"Clip production · generation {row.generation}",
                    status=row.status,
                    stage=row.stage,
                    state=state,
                    message=message,
                    updated_at=row.updated_at,
                    href=_item_href(row.channel_profile_id),
                    recovery_action=recovery_action,
                    recovery_label=recovery_label,
                )
            )

        for row in compilations:
            if row.channel_profile_id is None:
                continue
            state = _work_state(
                status=row.status,
                error=row.error,
                render_status=None,
            )
            if state is None:
                continue
            recovery_action, recovery_label = _work_recovery(
                kind="compilation",
                status=row.status,
                stage=row.stage,
                state=state,
                row=row,
            )
            work_items.append(
                OperationsWorkItem(
                    kind="compilation",
                    id=row.id,
                    channel_profile_id=row.channel_profile_id,
                    title=f"Long-form compilation · {row.theme}",
                    status=row.status,
                    stage=row.stage,
                    state=state,
                    message=row.error or f"{row.stage} · {row.status}",
                    updated_at=row.updated_at,
                    href=_item_href(row.channel_profile_id),
                    recovery_action=recovery_action,
                    recovery_label=recovery_label,
                )
            )

        publications_all = list(
            session.scalars(
                select(Publication)
                .where(
                    Publication.youtube_connection_id.in_(list(youtube_to_channel)),
                    ~Publication.status.in_(_DONE_PUBLICATION_STATUSES),
                )
                .order_by(Publication.updated_at.desc())
            )
        )
        for row in publications_all:
            channel_id = youtube_to_channel.get(row.youtube_connection_id)
            if channel_id is None:
                continue
            normalized = str(row.status or "").lower()
            if row.error or row.failure_reason or normalized == "failed":
                state: WorkState | None = "attention"
            elif normalized in _DONE_PUBLICATION_STATUSES:
                state = None
            else:
                state = "active"
            if state is None:
                continue
            recovery_action, recovery_label = _work_recovery(
                kind="publication",
                status=row.status,
                stage=row.stage,
                state=state,
                row=row,
            )
            work_items.append(
                OperationsWorkItem(
                    kind="publication",
                    id=row.id,
                    channel_profile_id=channel_id,
                    title=row.title,
                    status=row.status,
                    stage=row.stage,
                    state=state,
                    message=row.failure_reason or row.error or f"{row.stage} · {row.status}",
                    updated_at=row.updated_at,
                    href=_publication_href(channel_id),
                    recovery_action=recovery_action,
                    recovery_label=recovery_label,
                )
            )

        work_items.sort(key=lambda item: item.updated_at.isoformat(), reverse=True)
        attention = [item for item in work_items if item.state == "attention"][:limit]
        active = [item for item in work_items if item.state == "active"][:limit]

        latest_watch = (
            select(
                ChannelTrendWatchVersion.channel_profile_id.label("channel_profile_id"),
                func.max(ChannelTrendWatchVersion.version).label("version"),
            )
            .where(ChannelTrendWatchVersion.channel_profile_id.in_(channel_ids))
            .group_by(ChannelTrendWatchVersion.channel_profile_id)
            .subquery()
        )
        ranked_opportunities = (
            select(
                TrendOpportunity.id.label("id"),
                func.row_number()
                .over(
                    partition_by=(
                        TrendOpportunity.channel_profile_id,
                        TrendOpportunity.trend_topic_id,
                    ),
                    order_by=(
                        TrendOpportunity.created_at.desc(),
                        TrendOpportunity.id.desc(),
                    ),
                )
                .label("position"),
            )
            .join(
                latest_watch,
                and_(
                    TrendOpportunity.channel_profile_id
                    == latest_watch.c.channel_profile_id,
                    TrendOpportunity.watch_version == latest_watch.c.version,
                ),
            )
            .where(TrendOpportunity.expires_at > now)
            .subquery()
        )
        qualified_opportunity_rows = session.execute(
            select(TrendOpportunity, TrendTopic)
            .join(
                ranked_opportunities,
                ranked_opportunities.c.id == TrendOpportunity.id,
            )
            .join(TrendTopic, TrendTopic.id == TrendOpportunity.trend_topic_id)
            .where(ranked_opportunities.c.position == 1)
            .order_by(
                func.coalesce(
                    TrendOpportunity.calibrated_score,
                    TrendOpportunity.opportunity_score,
                ).desc(),
                TrendOpportunity.confidence.desc(),
                TrendOpportunity.id,
            )
        ).all()
        fresh_opportunity_count = len(qualified_opportunity_rows)
        opportunities = [
            OperationsOpportunity(
                id=row.id,
                channel_profile_id=row.channel_profile_id,
                topic=topic.display_name,
                lifecycle=row.lifecycle,
                opportunity_score=row.opportunity_score,
                confidence=row.confidence,
                expires_at=row.expires_at,
                reasons=list(row.reasons or [])[:3],
                href=f"/explorer?channel={row.channel_profile_id}",
            )
            for row, topic in qualified_opportunity_rows[:limit]
        ]

        recent_publications = list(
            session.scalars(
                select(Publication)
                .where(
                    Publication.youtube_connection_id.in_(list(youtube_to_channel)),
                    Publication.status.in_(_DONE_PUBLICATION_STATUSES),
                )
                .order_by(Publication.updated_at.desc())
                .limit(max(limit * 2, 12))
            )
        )
        publication_ids = [row.id for row in recent_publications]
        snapshot_by_publication: dict[uuid.UUID, PublicationAnalyticsSnapshot] = {}
        if publication_ids:
            snapshots = list(
                session.scalars(
                    select(PublicationAnalyticsSnapshot)
                    .where(PublicationAnalyticsSnapshot.publication_id.in_(publication_ids))
                    .order_by(PublicationAnalyticsSnapshot.sampled_at.desc())
                )
            )
            for snapshot in snapshots:
                snapshot_by_publication.setdefault(snapshot.publication_id, snapshot)

        publications: list[OperationsPublication] = []
        for row in recent_publications[:limit]:
            channel_id = youtube_to_channel.get(row.youtube_connection_id)
            if channel_id is None:
                continue
            snapshot = snapshot_by_publication.get(row.id)
            publications.append(
                OperationsPublication(
                    id=row.id,
                    channel_profile_id=channel_id,
                    title=row.title,
                    status=row.status,
                    youtube_video_id=row.youtube_video_id,
                    published_at=row.published_at,
                    sampled_at=snapshot.sampled_at if snapshot else None,
                    views=snapshot.views if snapshot else None,
                    average_view_percentage=(
                        snapshot.average_view_percentage if snapshot else None
                    ),
                    estimated_revenue=(
                        snapshot.estimated_revenue if snapshot else None
                    ),
                    href=_publication_href(channel_id),
                )
            )

        publication_channels = {
            row.id: youtube_to_channel[row.youtube_connection_id]
            for row in publications_all + recent_publications
            if row.youtube_connection_id in youtube_to_channel
        }
        event_filters = [
            and_(
                DomainEvent.aggregate_type == "channel_profile",
                DomainEvent.aggregate_id.in_([str(value) for value in channel_ids]),
            )
        ]
        if production_channels:
            event_filters.append(
                and_(
                    DomainEvent.aggregate_type == "production",
                    DomainEvent.aggregate_id.in_(
                        [str(value) for value in production_channels]
                    ),
                )
            )
        if episode_channels:
            event_filters.append(
                and_(
                    DomainEvent.aggregate_type == "short_episode",
                    DomainEvent.aggregate_id.in_(
                        [str(value) for value in episode_channels]
                    ),
                )
            )
        if compilation_channels:
            event_filters.append(
                and_(
                    DomainEvent.aggregate_type == "compilation",
                    DomainEvent.aggregate_id.in_(
                        [str(value) for value in compilation_channels]
                    ),
                )
            )
        if publication_channels:
            event_filters.append(
                and_(
                    DomainEvent.aggregate_type == "publication",
                    DomainEvent.aggregate_id.in_(
                        [str(value) for value in publication_channels]
                    ),
                )
            )
        recent_events = list(
            session.scalars(
                select(DomainEvent)
                .where(or_(*event_filters))
                .order_by(DomainEvent.created_at.desc())
                .limit(limit)
            )
        )
        visible_ids = set(channel_ids)
        activity: list[OperationsActivity] = []
        for event in recent_events:
            event_channel = _event_channel_id(
                event,
                production_channels=production_channels,
                episode_channels=episode_channels,
                compilation_channels=compilation_channels,
                publication_channels=publication_channels,
            )
            if event_channel is None or event_channel not in visible_ids:
                continue
            activity.append(
                OperationsActivity(
                    event_type=event.event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    channel_profile_id=event_channel,
                    created_at=event.created_at,
                    href=_event_href(event, event_channel),
                )
            )
            if len(activity) >= limit:
                break

        published_cutoff = now - timedelta(days=7)
        published_last_7d = list(
            session.scalars(
                select(Publication).where(
                    Publication.youtube_connection_id.in_(list(youtube_to_channel)),
                    Publication.published_at.is_not(None),
                    Publication.published_at >= published_cutoff,
                )
            )
        )

        attention_all = [item for item in work_items if item.state == "attention"]
        active_all = [item for item in work_items if item.state == "active"]
        opportunity_count_by_channel = {channel_id: 0 for channel_id in channel_ids}
        for row, _topic in qualified_opportunity_rows:
            opportunity_count_by_channel[row.channel_profile_id] += 1
        published_count_by_channel = {channel_id: 0 for channel_id in channel_ids}
        for row in published_last_7d:
            channel_id = youtube_to_channel.get(row.youtube_connection_id)
            if channel_id in published_count_by_channel:
                published_count_by_channel[channel_id] += 1
        attention_count_by_channel = {channel_id: 0 for channel_id in channel_ids}
        active_count_by_channel = {channel_id: 0 for channel_id in channel_ids}
        for item in attention_all:
            attention_count_by_channel[item.channel_profile_id] += 1
        for item in active_all:
            active_count_by_channel[item.channel_profile_id] += 1

        channel_summaries = [
            OperationsChannelSummary(
                id=channel_id,
                title=_channel_title(profile_by_id[channel_id]),
                status=profile_by_id[channel_id].status,
                timezone=profile_by_id[channel_id].timezone,
                active_work=active_count_by_channel[channel_id],
                needs_attention=attention_count_by_channel[channel_id],
                fresh_opportunities=opportunity_count_by_channel[channel_id],
                published_last_7d=published_count_by_channel[channel_id],
                href=f"/channels?channel={channel_id}",
            )
            for channel_id in channel_ids
        ]

        return OperationsOverviewResponse(
            generated_at=now,
            summary=OperationsSummary(
                active_channels=len(channel_ids),
                active_work=len(active_all),
                needs_attention=len(attention_all),
                fresh_opportunities=fresh_opportunity_count,
                published_last_7d=len(published_last_7d),
            ),
            channels=channel_summaries,
            intake=intake,
            attention=attention,
            active=active,
            opportunities=opportunities,
            publications=publications,
            activity=activity,
        )
