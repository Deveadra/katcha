from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from katcha.api.control_auth import (
    control_actor,
    control_allowed_channel_ids,
    control_principal_name,
    require_control_channel,
    require_control_scope,
)
from katcha.db import session_scope
from katcha.domain import AutomationLevel, CompilationStatus, ProductionStatus
from katcha.edit_performance_models import EditBlueprintPerformanceSnapshot
from katcha.intelligence.runtime import DEFAULT_REFRESH_INTERVAL_HOURS
from katcha.intelligence_models import (
    AutomationPolicyVersion,
    ChannelEconomicsSnapshot,
    ChannelProfile,
    ChannelStrategyVersion,
    RankingSnapshot,
    ScheduleRecommendation,
)
from katcha.longform_models import Compilation
from katcha.orchestration.client import (
    start_channel_intelligence_refresh,
    start_channel_intelligence_schedule,
    start_longform_workflow,
    start_production_workflow,
)
from katcha.packaging_intelligence_models import PackagingIntelligenceSnapshot
from katcha.production_models import Production
from katcha.services.automation_schedules import (
    mark_schedule_reconciled,
    register_channel_intelligence_schedule,
)
from katcha.services.channel_automation import (
    automation_summary,
    promote_automation,
)
from katcha.services.channel_economics import latest_economics_snapshot
from katcha.services.channel_editorial import (
    freeze_channel_compilation_candidates,
    score_clip_for_channel,
)
from katcha.services.channel_growth import channel_growth_context
from katcha.services.channel_learning import latest_ranking_snapshot
from katcha.services.channel_profiles import (
    active_strategy,
    create_strategy_version,
    ensure_active_profile,
    ensure_channel_profile,
    sync_channel_profile_metadata,
)
from katcha.services.channel_scheduling import latest_schedule_recommendations
from katcha.services.compilations import register_compilation
from katcha.services.edit_blueprint_performance import (
    latest_edit_blueprint_performance,
    list_edit_blueprint_performance,
)
from katcha.services.event_stream import (
    acknowledge_consumer_event,
    list_consumer_events,
)
from katcha.services.packaging_intelligence import (
    latest_packaging_intelligence,
    list_packaging_intelligence,
)
from katcha.services.productions import register_short_production

router = APIRouter(prefix="/v1", tags=["channel-intelligence"])


class ScheduleSlot(BaseModel):
    weekday: int = Field(ge=0, le=6)
    hour_local: int = Field(ge=0, le=23)


class CreateChannelRequest(BaseModel):
    youtube_connection_id: uuid.UUID
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    fallback_schedule: list[ScheduleSlot] = Field(default_factory=list, max_length=20)
    refresh_interval_hours: int = Field(
        default=DEFAULT_REFRESH_INTERVAL_HOURS,
        ge=1,
        le=168,
    )


class ChannelProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    youtube_connection_id: uuid.UUID
    status: str
    timezone: str
    active_strategy_version: int
    active_automation_version: int
    profile_metadata: dict[str, object]
    created_at: datetime
    updated_at: datetime


class StrategyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    monthly_base_budget_usd: Decimal
    monthly_hard_budget_usd: Decimal
    reinvestment_rate: Decimal
    reinvestment_cap_usd: Decimal
    fallback_schedule: list[dict[str, object]]
    blackout_windows: list[dict[str, object]]
    routing_policy: dict[str, object]
    strategy_metadata: dict[str, object]
    created_at: datetime


class UpdateStrategyRequest(BaseModel):
    monthly_base_budget_usd: Decimal | None = Field(default=None, ge=0)
    monthly_hard_budget_usd: Decimal | None = Field(default=None, ge=0)
    reinvestment_rate: Decimal | None = Field(default=None, ge=0, le=1)
    reinvestment_cap_usd: Decimal | None = Field(default=None, ge=0)
    fallback_schedule: list[ScheduleSlot] | None = Field(default=None, max_length=20)
    blackout_windows: list[ScheduleSlot] | None = Field(default=None, max_length=40)
    routing_policy: dict[str, object] | None = None
    actor: str = Field(default="operator", min_length=1, max_length=128)


class RankingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    run_key: str
    algorithm: str
    training_cutoff: datetime
    sample_count: int
    blend_ratio: Decimal
    confidence: Decimal
    validation_metrics: dict[str, object]
    created_at: datetime


class EconomicsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    sample_key: str
    sampled_at: datetime
    revenue_usd: Decimal
    attributed_ai_cost_usd: Decimal
    contribution_margin_usd: Decimal
    reinvestable_usd: Decimal
    base_budget_usd: Decimal
    hard_budget_usd: Decimal
    effective_budget_usd: Decimal
    month_to_date_spend_usd: Decimal
    reserved_ai_cost_usd: Decimal
    budget_headroom_usd: Decimal
    burn_rate_usd_per_day: Decimal
    projected_month_end_spend_usd: Decimal
    monetary_scope_available: bool
    details: dict[str, object]


class EditBlueprintPerformanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    run_key: str
    age_bucket_hours: int
    publication_count: int
    blueprint_group_count: int
    revenue_covered_publications: int
    retention_covered_publications: int
    monetary_coverage: Decimal
    retention_coverage: Decimal
    aggregate_metrics: list[dict[str, object]]
    comparison_status: str
    comparison_summary: dict[str, object]
    sample_window_start: date | None
    sample_window_end: date | None
    created_at: datetime


class PackagingIntelligenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    run_key: str
    maturity_days: int
    publication_count: int
    variant_window_count: int
    recommendation_count: int
    recommendation_status: str
    variant_metrics: list[dict[str, object]]
    recommendations: list[dict[str, object]]
    validation_metrics: dict[str, object]
    policy_snapshot: dict[str, object]
    sample_window_start: datetime | None
    sample_window_end: datetime | None
    created_at: datetime


class ScheduleRecommendationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rank: int
    weekday: int
    hour_local: int
    score: Decimal
    sample_count: int
    confidence: Decimal
    source: str
    recommendation_metadata: dict[str, object]


class GrowthTargetRequest(BaseModel):
    metric: Literal[
        "subscribers",
        "qualified_watch_hours_365d",
        "qualified_shorts_views_90d",
        "public_uploads_90d",
    ]
    target: Decimal = Field(gt=0)
    target_date: date | None = None
    priority: int = Field(default=3, ge=1, le=5)
    enabled: bool = True


class GrowthGoalsRequest(BaseModel):
    objective: Literal["early_ypp", "ads_revenue", "channel_growth"] = "ads_revenue"
    path: Literal["fastest", "shorts", "long_form", "balanced"] = "fastest"
    pace: Literal["aggressive", "balanced"] = "aggressive"
    target_date: date | None = None
    custom_targets: list[GrowthTargetRequest] = Field(default_factory=list, max_length=20)
    actor: str = Field(default="operator", min_length=1, max_length=128)


class RefreshIntelligenceRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=256)


class RefreshIntelligenceResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    run_key: str


class IntelligenceScheduleRequest(BaseModel):
    interval_hours: int = Field(
        default=DEFAULT_REFRESH_INTERVAL_HOURS,
        ge=1,
        le=168,
    )


class IntelligenceScheduleResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    interval_hours: int
    generation: int


class PromoteAutomationRequest(BaseModel):
    target_level: Literal[
        "auto_approve_low_risk",
        "auto_publish_private",
        "auto_publish_scheduled",
    ]
    actor: str = Field(default="operator", min_length=1, max_length=128)


class AutomationPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    level: str
    policy_metadata: dict[str, object]
    created_at: datetime


class CreateChannelProductionRequest(BaseModel):
    persona_key: str = Field(default="youth_host", min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=256)
    edit_blueprint_key: str | None = Field(default=None, min_length=1, max_length=96)


class ChannelProductionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    clip_id: uuid.UUID
    channel_profile_id: uuid.UUID | None
    edit_blueprint_key: str | None
    edit_blueprint_version: int | None
    workflow_id: str
    status: str
    stage: str
    generation: int
    created_at: datetime


class CreateChannelCompilationRequest(BaseModel):
    theme: str = Field(min_length=1, max_length=500)
    target_duration_seconds: int | None = Field(default=None, ge=180, le=7200)
    target_segment_count: int | None = Field(default=None, ge=3, le=100)
    persona_key: str = Field(default="youth_host", min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=256)


class ChannelCompilationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID | None
    workflow_id: str
    status: str
    stage: str
    generation: int
    theme: str
    candidate_snapshot: dict[str, object]
    created_at: datetime


class ClipChannelScoreResponse(BaseModel):
    channel_profile_id: uuid.UUID
    clip_id: uuid.UUID
    score: float
    features: dict[str, float]
    details: dict[str, object]


class EventEnvelope(BaseModel):
    id: uuid.UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, object]
    created_at: datetime


class AckEventRequest(BaseModel):
    consumer_key: str = Field(min_length=1, max_length=128)
    channel_profile_id: uuid.UUID | None = None
    metadata: dict[str, object] | None = None


class EventCursorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    consumer_key: str
    last_event_id: uuid.UUID | None
    last_event_created_at: datetime | None
    cursor_metadata: dict[str, object]
    updated_at: datetime


def _slots(items: list[ScheduleSlot] | None) -> list[dict[str, object]] | None:
    if items is None:
        return None
    return [item.model_dump() for item in items]


def _refresh_identity(
    channel_profile_id: uuid.UUID,
    key: str | None,
) -> tuple[str, str]:
    run_key = key.strip() if key and key.strip() else f"manual-{uuid.uuid4().hex}"
    digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:20]
    return run_key, f"channel-intelligence-refresh-{channel_profile_id}-{digest}"


@router.post(
    "/channels",
    response_model=ChannelProfileResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_channel(
    http_request: Request,
    request: CreateChannelRequest,
) -> ChannelProfile:
    require_control_scope(http_request, "channels:write")
    if control_allowed_channel_ids(http_request) is not None:
        raise HTTPException(
            status_code=403,
            detail="channel-restricted principals cannot create channel profiles",
        )
    try:
        profile = ensure_channel_profile(
            request.youtube_connection_id,
            timezone=request.timezone,
            fallback_schedule=_slots(request.fallback_schedule),
        )
        registration = register_channel_intelligence_schedule(
            profile.id,
            interval_hours=request.refresh_interval_hours,
            replace_existing=False,
        )
        await start_channel_intelligence_schedule(
            str(profile.id),
            registration.schedule.workflow_id,
            interval_hours=int(
                registration.schedule.schedule_config["interval_hours"]
            ),
            supersedes_workflow_id=registration.supersedes_workflow_id,
        )
        mark_schedule_reconciled(
            registration.schedule.id,
            registration.schedule.workflow_id,
        )
        return profile
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/channels", response_model=list[ChannelProfileResponse])
def list_channels(http_request: Request) -> list[ChannelProfile]:
    require_control_scope(http_request, "channels:read")
    allowed = control_allowed_channel_ids(http_request)
    with session_scope() as session:
        stmt = select(ChannelProfile).order_by(ChannelProfile.created_at)
        if allowed is not None:
            stmt = stmt.where(ChannelProfile.id.in_(sorted(allowed, key=str)))
        return list(session.scalars(stmt))


@router.get("/channels/{channel_profile_id}")
def get_channel_summary(
    channel_profile_id: uuid.UUID,
    http_request: Request,
) -> dict[str, object]:
    require_control_scope(http_request, "channels:read")
    try:
        with session_scope() as session:
            profile = ensure_active_profile(session, channel_profile_id)
            sync_channel_profile_metadata(session, profile)
            strategy = active_strategy(session, profile)
            ranking = latest_ranking_snapshot(session, profile)
            economics = latest_economics_snapshot(session, profile)
            edit_performance = latest_edit_blueprint_performance(channel_profile_id)
            packaging_intelligence = latest_packaging_intelligence(channel_profile_id)
            profile_payload = ChannelProfileResponse.model_validate(profile).model_dump(mode="json")
            strategy_payload = StrategyResponse.model_validate(strategy).model_dump(mode="json")
            ranking_payload = (
                RankingResponse.model_validate(ranking).model_dump(mode="json")
                if ranking
                else None
            )
            economics_payload = (
                EconomicsResponse.model_validate(economics).model_dump(mode="json")
                if economics
                else None
            )
        growth = channel_growth_context(channel_profile_id)
        return {
            "profile": profile_payload,
            "strategy": strategy_payload,
            "ranking": ranking_payload,
            "economics": economics_payload,
            "edit_performance": (
                EditBlueprintPerformanceResponse.model_validate(
                    edit_performance
                ).model_dump(mode="json")
                if edit_performance
                else None
            ),
            "packaging_intelligence": (
                PackagingIntelligenceResponse.model_validate(
                    packaging_intelligence
                ).model_dump(mode="json")
                if packaging_intelligence
                else None
            ),
            "schedule": [
                ScheduleRecommendationResponse.model_validate(item).model_dump(mode="json")
                for item in latest_schedule_recommendations(channel_profile_id)
            ],
            "automation": automation_summary(channel_profile_id),
            "growth": growth,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/strategy",
    response_model=StrategyResponse,
)
def update_channel_strategy(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    request: UpdateStrategyRequest,
) -> ChannelStrategyVersion:
    require_control_scope(http_request, "intelligence:write")
    try:
        return create_strategy_version(
            channel_profile_id,
            monthly_base_budget_usd=request.monthly_base_budget_usd,
            monthly_hard_budget_usd=request.monthly_hard_budget_usd,
            reinvestment_rate=request.reinvestment_rate,
            reinvestment_cap_usd=request.reinvestment_cap_usd,
            fallback_schedule=_slots(request.fallback_schedule),
            blackout_windows=_slots(request.blackout_windows),
            routing_policy=request.routing_policy,
            actor=control_actor(http_request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/channels/{channel_profile_id}/growth")
def get_channel_growth(
    channel_profile_id: uuid.UUID,
    http_request: Request,
) -> dict[str, object]:
    require_control_scope(http_request, "channels:read")
    try:
        return channel_growth_context(channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/growth-goals",
    response_model=StrategyResponse,
)
def update_channel_growth_goals(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    request: GrowthGoalsRequest,
) -> ChannelStrategyVersion:
    require_control_scope(http_request, "intelligence:write")
    try:
        growth_strategy = request.model_dump(mode="json", exclude={"actor"})
        return create_strategy_version(
            channel_profile_id,
            growth_strategy=growth_strategy,
            actor=control_actor(http_request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/intelligence/schedule",
    response_model=IntelligenceScheduleResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def schedule_channel_intelligence(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    request: IntelligenceScheduleRequest,
) -> IntelligenceScheduleResponse:
    require_control_scope(http_request, "intelligence:write")
    try:
        with session_scope() as session:
            ensure_active_profile(session, channel_profile_id)
        registration = register_channel_intelligence_schedule(
            channel_profile_id,
            interval_hours=request.interval_hours,
        )
        await start_channel_intelligence_schedule(
            str(channel_profile_id),
            registration.schedule.workflow_id,
            interval_hours=request.interval_hours,
            supersedes_workflow_id=registration.supersedes_workflow_id,
        )
        mark_schedule_reconciled(
            registration.schedule.id,
            registration.schedule.workflow_id,
        )
        return IntelligenceScheduleResponse(
            channel_profile_id=channel_profile_id,
            workflow_id=registration.schedule.workflow_id,
            interval_hours=request.interval_hours,
            generation=registration.schedule.generation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/intelligence/refresh",
    response_model=RefreshIntelligenceResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_channel_intelligence(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    request: RefreshIntelligenceRequest,
) -> RefreshIntelligenceResponse:
    require_control_scope(http_request, "intelligence:write")
    try:
        with session_scope() as session:
            ensure_active_profile(session, channel_profile_id)
        run_key, workflow_id = _refresh_identity(
            channel_profile_id,
            request.idempotency_key,
        )
        await start_channel_intelligence_refresh(
            str(channel_profile_id),
            workflow_id,
            run_key,
        )
        return RefreshIntelligenceResponse(
            channel_profile_id=channel_profile_id,
            workflow_id=workflow_id,
            run_key=run_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/channels/{channel_profile_id}/ranking",
    response_model=RankingResponse | None,
)
def get_channel_ranking(
    channel_profile_id: uuid.UUID,
    http_request: Request,
) -> RankingSnapshot | None:
    require_control_scope(http_request, "channels:read")
    with session_scope() as session:
        try:
            profile = ensure_active_profile(session, channel_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return latest_ranking_snapshot(session, profile)


@router.get(
    "/channels/{channel_profile_id}/economics",
    response_model=EconomicsResponse | None,
)
def get_channel_economics(
    channel_profile_id: uuid.UUID,
    http_request: Request,
) -> ChannelEconomicsSnapshot | None:
    require_control_scope(http_request, "channels:read")
    with session_scope() as session:
        try:
            profile = ensure_active_profile(session, channel_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return latest_economics_snapshot(session, profile)


@router.get(
    "/channels/{channel_profile_id}/editing-performance",
    response_model=EditBlueprintPerformanceResponse | None,
)
def get_channel_editing_performance(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    age_bucket_hours: int | None = Query(default=None),
) -> EditBlueprintPerformanceSnapshot | None:
    require_control_scope(http_request, "channels:read")
    with session_scope() as session:
        try:
            ensure_active_profile(session, channel_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return latest_edit_blueprint_performance(
        channel_profile_id,
        age_bucket_hours=age_bucket_hours,
    )


@router.get(
    "/channels/{channel_profile_id}/editing-performance/history",
    response_model=list[EditBlueprintPerformanceResponse],
)
def get_channel_editing_performance_history(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    limit: int = Query(default=50, ge=1, le=250),
    age_bucket_hours: int | None = Query(default=None),
) -> list[EditBlueprintPerformanceSnapshot]:
    require_control_scope(http_request, "channels:read")
    with session_scope() as session:
        try:
            ensure_active_profile(session, channel_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return list_edit_blueprint_performance(
        channel_profile_id,
        limit=limit,
        age_bucket_hours=age_bucket_hours,
    )


@router.get(
    "/channels/{channel_profile_id}/packaging-intelligence",
    response_model=PackagingIntelligenceResponse | None,
)
def get_channel_packaging_intelligence(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    maturity_days: int | None = Query(default=None),
) -> PackagingIntelligenceSnapshot | None:
    require_control_scope(http_request, "channels:read")
    with session_scope() as session:
        try:
            ensure_active_profile(session, channel_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return latest_packaging_intelligence(
        channel_profile_id,
        maturity_days=maturity_days,
    )


@router.get(
    "/channels/{channel_profile_id}/packaging-intelligence/history",
    response_model=list[PackagingIntelligenceResponse],
)
def get_channel_packaging_intelligence_history(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    limit: int = Query(default=50, ge=1, le=250),
    maturity_days: int | None = Query(default=None),
) -> list[PackagingIntelligenceSnapshot]:
    require_control_scope(http_request, "channels:read")
    with session_scope() as session:
        try:
            ensure_active_profile(session, channel_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return list_packaging_intelligence(
        channel_profile_id,
        maturity_days=maturity_days,
        limit=limit,
    )


@router.get(
    "/channels/{channel_profile_id}/schedule",
    response_model=list[ScheduleRecommendationResponse],
)
def get_channel_schedule(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    limit: int = Query(default=5, ge=1, le=20),
) -> list[ScheduleRecommendation]:
    require_control_scope(http_request, "channels:read")
    try:
        return latest_schedule_recommendations(channel_profile_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/channels/{channel_profile_id}/automation")
def get_channel_automation(
    channel_profile_id: uuid.UUID,
    http_request: Request,
) -> dict[str, object]:
    require_control_scope(http_request, "channels:read")
    try:
        return automation_summary(channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/automation/promote",
    response_model=AutomationPolicyResponse,
)
def promote_channel_automation(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    request: PromoteAutomationRequest,
) -> AutomationPolicyVersion:
    require_control_scope(http_request, "intelligence:write")
    try:
        return promote_automation(
            channel_profile_id,
            target_level=AutomationLevel(request.target_level),
            actor=control_actor(http_request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/channels/{channel_profile_id}/clips/{clip_id}/score",
    response_model=ClipChannelScoreResponse,
)
def get_clip_channel_score(
    channel_profile_id: uuid.UUID,
    clip_id: uuid.UUID,
    http_request: Request,
) -> ClipChannelScoreResponse:
    require_control_scope(http_request, "channels:read")
    try:
        payload = score_clip_for_channel(channel_profile_id, clip_id)
        return ClipChannelScoreResponse.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/clips/{clip_id}/productions",
    response_model=ChannelProductionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_channel_production(
    channel_profile_id: uuid.UUID,
    clip_id: uuid.UUID,
    http_request: Request,
    request: CreateChannelProductionRequest,
) -> Production:
    require_control_scope(http_request, "production:create")
    try:
        production = register_short_production(
            clip_id,
            persona_key=request.persona_key,
            idempotency_key=request.idempotency_key,
            channel_profile_id=channel_profile_id,
            edit_blueprint_key=request.edit_blueprint_key,
        )
        if production.status == ProductionStatus.QUEUED.value:
            await start_production_workflow(
                str(production.id),
                production.workflow_id,
            )
        return production
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/channels/{channel_profile_id}/compilations",
    response_model=ChannelCompilationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_channel_compilation(
    channel_profile_id: uuid.UUID,
    http_request: Request,
    request: CreateChannelCompilationRequest,
) -> Compilation:
    require_control_scope(http_request, "production:create")
    try:
        compilation = register_compilation(
            theme=request.theme,
            target_duration_seconds=request.target_duration_seconds,
            target_segment_count=request.target_segment_count,
            persona_key=request.persona_key,
            idempotency_key=request.idempotency_key,
            channel_profile_id=channel_profile_id,
        )
        if compilation.status == CompilationStatus.QUEUED.value:
            compilation = freeze_channel_compilation_candidates(compilation.id)
            await start_longform_workflow(
                str(compilation.id),
                compilation.workflow_id,
                start_stage="select",
            )
        return compilation
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/control/events", response_model=list[EventEnvelope])
def control_events(
    http_request: Request,
    consumer_key: str = Query(min_length=1, max_length=128),
    channel_profile_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, object]]:
    require_control_scope(http_request, "events:read")
    allowed = control_allowed_channel_ids(http_request)
    if allowed is not None and channel_profile_id is None:
        raise HTTPException(
            status_code=403,
            detail="channel-restricted principals must select a channel event stream",
        )
    if channel_profile_id is not None:
        require_control_channel(http_request, channel_profile_id)
    actor = (
        control_actor(http_request)
        if control_principal_name(http_request) is not None
        else None
    )
    try:
        return list_consumer_events(
            consumer_key,
            channel_profile_id=channel_profile_id,
            consumer_actor=actor,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/control/events/{event_id}/ack",
    response_model=EventCursorResponse,
)
def acknowledge_control_event(
    event_id: uuid.UUID,
    http_request: Request,
    request: AckEventRequest,
):
    require_control_scope(http_request, "events:ack")
    allowed = control_allowed_channel_ids(http_request)
    if allowed is not None and request.channel_profile_id is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "channel-restricted principals must acknowledge within a "
                "channel stream"
            ),
        )
    if request.channel_profile_id is not None:
        require_control_channel(http_request, request.channel_profile_id)
    actor = (
        control_actor(http_request)
        if control_principal_name(http_request) is not None
        else None
    )
    try:
        return acknowledge_consumer_event(
            request.consumer_key,
            event_id,
            channel_profile_id=request.channel_profile_id,
            consumer_actor=actor,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
