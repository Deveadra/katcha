from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.orchestration.client import start_trend_calibration_workflow
from katcha.services.trend_calibration import (
    calibration_summary,
    opportunity_outcomes,
)
from katcha.trend_calibration_models import TrendOpportunityOutcome

router = APIRouter(prefix="/trends", tags=["trend-calibration"])


class TrendCalibrationRefreshRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=160)
    target_age_hours: int = Field(default=24)


class TrendCalibrationRefreshResponse(BaseModel):
    channel_profile_id: uuid.UUID
    workflow_id: str
    run_key: str
    target_age_hours: int


class TrendOutcomeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel_profile_id: uuid.UUID
    trend_opportunity_id: uuid.UUID
    publication_id: uuid.UUID
    analytics_snapshot_id: uuid.UUID
    age_bucket_hours: int
    opportunity_created_at: datetime
    published_at: datetime
    sampled_at: datetime
    lead_time_hours: Decimal
    predicted_score: Decimal
    predicted_confidence: Decimal
    predicted_lifecycle: str
    prediction_components: dict[str, float]
    observed_outcome_score: Decimal
    baseline_sample_count: int
    baseline_outcome_score: Decimal | None
    lift_ratio: Decimal | None
    realized_breakout: bool | None
    outcome_metrics: dict[str, Any]
    created_at: datetime


def _refresh_identity(
    channel_profile_id: uuid.UUID,
    key: str | None,
    target_age_hours: int,
) -> tuple[str, str]:
    run_key = key.strip() if key and key.strip() else f"manual-{uuid.uuid4().hex}"
    digest = hashlib.sha256(
        f"{run_key}:{target_age_hours}".encode()
    ).hexdigest()[:20]
    workflow_id = f"trend-calibration-{channel_profile_id}-{digest}"
    return run_key, workflow_id


@router.post(
    "/channels/{channel_profile_id}/calibration/refresh",
    response_model=TrendCalibrationRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_channel_trend_calibration(
    channel_profile_id: uuid.UUID,
    request: TrendCalibrationRefreshRequest,
) -> TrendCalibrationRefreshResponse:
    if request.target_age_hours not in {6, 24, 72, 168}:
        raise HTTPException(
            status_code=400,
            detail="target_age_hours must be one of 6, 24, 72, 168",
        )
    run_key, workflow_id = _refresh_identity(
        channel_profile_id,
        request.idempotency_key,
        request.target_age_hours,
    )
    started = await start_trend_calibration_workflow(
        str(channel_profile_id),
        workflow_id,
        run_key,
        target_age_hours=request.target_age_hours,
    )
    return TrendCalibrationRefreshResponse(
        channel_profile_id=channel_profile_id,
        workflow_id=started,
        run_key=run_key,
        target_age_hours=request.target_age_hours,
    )


@router.get("/channels/{channel_profile_id}/calibration")
def get_channel_trend_calibration(
    channel_profile_id: uuid.UUID,
) -> dict[str, Any]:
    try:
        return calibration_summary(channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/opportunities/{opportunity_id}/outcomes",
    response_model=list[TrendOutcomeResponse],
)
def get_trend_opportunity_outcomes(
    opportunity_id: uuid.UUID,
) -> list[TrendOpportunityOutcome]:
    return opportunity_outcomes(opportunity_id)
