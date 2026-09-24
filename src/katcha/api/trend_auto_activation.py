from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.orchestration.client import (
    start_channel_trend_activation,
    start_channel_trend_activation_performance,
    start_channel_trend_activation_schedule,
)
from katcha.services.trend_activation_performance import (
    latest_activation_performance,
    list_activation_performance,
)
from katcha.services.trend_auto_activation import (
    active_activation_policy,
    create_activation_policy,
    list_activation_decisions,
    list_activation_runs,
)
from katcha.trend_activation_models import (
    TrendActivationDecision,
    TrendActivationPerformanceSnapshot,
    TrendActivationPolicyVersion,
    TrendActivationRun,
)

router = APIRouter(tags=["trend-auto-activation"])


class TrendActivationPolicyRequest(BaseModel):
    enabled: bool = False
    min_opportunity_score: float = Field(default=0.65, ge=0, le=1)
    min_confidence: float = Field(default=0.55, ge=0, le=1)
    min_calibrated_score: float | None = Field(default=None, ge=0, le=1)
    min_rights_readiness: float = Field(default=0.50, ge=0, le=1)
    min_lead_time_minutes: int = Field(default=120, ge=0, le=10080)
    max_activations_per_day: int = Field(default=3, ge=0, le=100)
    cooldown_minutes: int = Field(default=120, ge=0, le=10080)
    max_backlog: int = Field(default=5, ge=0, le=1000)
    min_budget_headroom_usd: Decimal = Field(default=Decimal("2.00"), ge=0)
    max_opportunities_per_run: int = Field(default=25, ge=1, le=250)
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: str = Field(default="operator", min_length=1, max_length=128)


class TrendActivationPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    enabled: bool
    min_opportunity_score: Decimal
    min_confidence: Decimal
    min_calibrated_score: Decimal | None
    min_rights_readiness: Decimal
    min_lead_time_minutes: int
    max_activations_per_day: int
    cooldown_minutes: int
    max_backlog: int
    min_budget_headroom_usd: Decimal
    max_opportunities_per_run: int
    policy_metadata: dict[str, Any]
    created_at: datetime


class TrendActivationScanRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)


class TrendActivationScanResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    run_key: str


class TrendActivationScheduleRequest(BaseModel):
    interval_hours: int = Field(default=1, ge=1, le=168)


class TrendActivationScheduleResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    interval_hours: int


class TrendActivationRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    policy_version: int | None
    run_key: str
    status: str
    inspected_count: int
    activated_count: int
    deferred_count: int
    skipped_count: int
    run_metadata: dict[str, Any]
    started_at: datetime
    completed_at: datetime | None


class TrendActivationDecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    activation_run_id: uuid.UUID
    channel_profile_id: uuid.UUID
    trend_opportunity_id: uuid.UUID
    trend_evidence_packet_id: uuid.UUID | None
    short_episode_id: uuid.UUID | None
    activation_key: str | None
    decision: str
    reason: str
    opportunity_score: Decimal
    confidence: Decimal
    calibrated_score: Decimal | None
    rights_readiness: Decimal
    remaining_lead_minutes: int
    decision_snapshot: dict[str, Any]
    created_at: datetime


def _scan_identity(
    channel_profile_id: uuid.UUID,
    idempotency_key: str | None,
) -> tuple[str, str]:
    run_key = (
        idempotency_key.strip()
        if idempotency_key and idempotency_key.strip()
        else f"manual-{uuid.uuid4().hex}"
    )
    digest = hashlib.sha256(run_key.encode()).hexdigest()[:20]
    return run_key, f"trend-auto-activation-{channel_profile_id}-{digest}"


@router.post(
    "/channels/{channel_profile_id}/trends/activation-policy",
    response_model=TrendActivationPolicyResponse,
)
def update_trend_activation_policy(
    channel_profile_id: uuid.UUID,
    request: TrendActivationPolicyRequest,
) -> TrendActivationPolicyVersion:
    try:
        return create_activation_policy(
            channel_profile_id,
            enabled=request.enabled,
            min_opportunity_score=request.min_opportunity_score,
            min_confidence=request.min_confidence,
            min_calibrated_score=request.min_calibrated_score,
            min_rights_readiness=request.min_rights_readiness,
            min_lead_time_minutes=request.min_lead_time_minutes,
            max_activations_per_day=request.max_activations_per_day,
            cooldown_minutes=request.cooldown_minutes,
            max_backlog=request.max_backlog,
            min_budget_headroom_usd=request.min_budget_headroom_usd,
            max_opportunities_per_run=request.max_opportunities_per_run,
            metadata=request.metadata,
            actor=request.actor,
        )
    except ValueError as exc:
        code = 404 if "channel profile not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get(
    "/channels/{channel_profile_id}/trends/activation-policy",
    response_model=TrendActivationPolicyResponse | None,
)
def get_trend_activation_policy(
    channel_profile_id: uuid.UUID,
) -> TrendActivationPolicyVersion | None:
    return active_activation_policy(channel_profile_id)


@router.post(
    "/channels/{channel_profile_id}/trends/activation-scan",
    response_model=TrendActivationScanResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def scan_trend_activation(
    channel_profile_id: uuid.UUID,
    request: TrendActivationScanRequest,
) -> TrendActivationScanResponse:
    policy = active_activation_policy(channel_profile_id)
    if policy is None:
        raise HTTPException(
            status_code=409,
            detail="trend activation policy is not configured",
        )
    run_key, workflow_id = _scan_identity(
        channel_profile_id,
        request.idempotency_key,
    )
    await start_channel_trend_activation(
        str(channel_profile_id),
        workflow_id,
        run_key,
    )
    return TrendActivationScanResponse(
        channel_profile_id=channel_profile_id,
        workflow_id=workflow_id,
        run_key=run_key,
    )


@router.post(
    "/channels/{channel_profile_id}/trends/activation-schedule",
    response_model=TrendActivationScheduleResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def schedule_trend_activation(
    channel_profile_id: uuid.UUID,
    request: TrendActivationScheduleRequest,
) -> TrendActivationScheduleResponse:
    policy = active_activation_policy(channel_profile_id)
    if policy is None:
        raise HTTPException(
            status_code=409,
            detail="trend activation policy is not configured",
        )
    workflow_id = f"trend-auto-activation-schedule-{channel_profile_id}"
    await start_channel_trend_activation_schedule(
        str(channel_profile_id),
        workflow_id,
        interval_hours=request.interval_hours,
    )
    return TrendActivationScheduleResponse(
        channel_profile_id=channel_profile_id,
        workflow_id=workflow_id,
        interval_hours=request.interval_hours,
    )


@router.get(
    "/channels/{channel_profile_id}/trends/activation-runs",
    response_model=list[TrendActivationRunResponse],
)
def get_trend_activation_runs(
    channel_profile_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=250),
) -> list[TrendActivationRun]:
    return list_activation_runs(channel_profile_id, limit=limit)


@router.get(
    "/trends/activation-runs/{run_id}/decisions",
    response_model=list[TrendActivationDecisionResponse],
)
def get_trend_activation_decisions(
    run_id: uuid.UUID,
) -> list[TrendActivationDecision]:
    try:
        return list_activation_decisions(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc



class TrendActivationPerformanceRefreshRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)


class TrendActivationPerformanceRefreshResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    run_key: str


class TrendActivationPerformanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    version: int
    run_key: str
    policy_version: int | None
    calibration_version: int | None
    economics_snapshot_id: uuid.UUID | None
    decision_count: int
    planned_count: int
    published_count: int
    outcome_count: int
    opportunity_to_plan_rate: Decimal
    plan_to_publish_rate: Decimal
    median_opportunity_to_plan_minutes: Decimal | None
    median_plan_to_publish_minutes: Decimal | None
    median_opportunity_to_publish_minutes: Decimal | None
    revenue_usd: Decimal
    attributed_cost_usd: Decimal
    contribution_margin_usd: Decimal
    latest_views: int
    mean_lift_ratio: Decimal | None
    realized_breakout_rate: Decimal | None
    missed_reason_counts: dict[str, int]
    funnel_metrics: dict[str, Any]
    recommendation_status: str
    recommendation: dict[str, Any]
    sample_window_start: datetime | None
    sample_window_end: datetime | None
    created_at: datetime


def _performance_identity(
    channel_profile_id: uuid.UUID,
    idempotency_key: str | None,
) -> tuple[str, str]:
    run_key = (
        idempotency_key.strip()
        if idempotency_key and idempotency_key.strip()
        else f"manual-{uuid.uuid4().hex}"
    )
    digest = hashlib.sha256(run_key.encode()).hexdigest()[:20]
    return run_key, f"trend-activation-performance-{channel_profile_id}-{digest}"


@router.post(
    "/channels/{channel_profile_id}/trends/activation-performance/refresh",
    response_model=TrendActivationPerformanceRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_trend_activation_performance(
    channel_profile_id: uuid.UUID,
    request: TrendActivationPerformanceRefreshRequest,
) -> TrendActivationPerformanceRefreshResponse:
    run_key, workflow_id = _performance_identity(
        channel_profile_id,
        request.idempotency_key,
    )
    await start_channel_trend_activation_performance(
        str(channel_profile_id),
        workflow_id,
        run_key,
    )
    return TrendActivationPerformanceRefreshResponse(
        channel_profile_id=channel_profile_id,
        workflow_id=workflow_id,
        run_key=run_key,
    )


@router.get(
    "/channels/{channel_profile_id}/trends/activation-performance",
    response_model=TrendActivationPerformanceResponse | None,
)
def get_latest_trend_activation_performance(
    channel_profile_id: uuid.UUID,
) -> TrendActivationPerformanceSnapshot | None:
    return latest_activation_performance(channel_profile_id)


@router.get(
    "/channels/{channel_profile_id}/trends/activation-performance/history",
    response_model=list[TrendActivationPerformanceResponse],
)
def get_trend_activation_performance_history(
    channel_profile_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=250),
) -> list[TrendActivationPerformanceSnapshot]:
    return list_activation_performance(channel_profile_id, limit=limit)
