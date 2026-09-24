from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from katcha.services.trend_activation import (
    ActivationPreview,
    activate_trend_opportunity,
    preview_trend_activation,
)

router = APIRouter(tags=["trend-activation"])


class ActivationCandidateResponse(BaseModel):
    discovery_candidate_id: uuid.UUID
    source_item_id: uuid.UUID
    clip_id: uuid.UUID
    candidate_score: float
    signals: dict[str, float]
    derivation: dict[str, Any]


class ActivationExclusionResponse(BaseModel):
    reference: str
    reason: str
    discovery_candidate_id: uuid.UUID | None = None
    source_item_id: uuid.UUID | None = None
    clip_id: uuid.UUID | None = None


class TrendActivationPreviewResponse(BaseModel):
    channel_profile_id: uuid.UUID
    trend_opportunity_id: uuid.UUID
    evidence_packet_id: uuid.UUID
    evidence_packet_sha256: str
    premise: str
    format_key: str
    format_version: str
    item_count: int
    activation_key: str
    ready: bool
    readiness_reason: str
    eligible: list[ActivationCandidateResponse]
    excluded: list[ActivationExclusionResponse]
    ordered_clip_ids: list[uuid.UUID]


class ActivateTrendOpportunityRequest(BaseModel):
    item_count: int | None = Field(default=None, ge=1, le=20)
    actor: str = Field(default="operator", min_length=1, max_length=128)


class ActivateTrendOpportunityResponse(BaseModel):
    short_episode_id: uuid.UUID
    status: str
    preview: TrendActivationPreviewResponse


def _preview_response(preview: ActivationPreview) -> TrendActivationPreviewResponse:
    return TrendActivationPreviewResponse(
        channel_profile_id=preview.channel_profile_id,
        trend_opportunity_id=preview.trend_opportunity_id,
        evidence_packet_id=preview.evidence_packet_id,
        evidence_packet_sha256=preview.evidence_packet_sha256,
        premise=preview.premise,
        format_key=preview.format_key,
        format_version=preview.format_version,
        item_count=preview.item_count,
        activation_key=preview.activation_key,
        ready=preview.ready,
        readiness_reason=preview.readiness_reason,
        eligible=[
            ActivationCandidateResponse(
                discovery_candidate_id=item.discovery_candidate_id,
                source_item_id=item.source_item_id,
                clip_id=item.clip_id,
                candidate_score=item.candidate_score,
                signals=item.signals.snapshot(),
                derivation=item.derivation,
            )
            for item in preview.eligible
        ],
        excluded=[
            ActivationExclusionResponse(
                reference=item.reference,
                reason=item.reason,
                discovery_candidate_id=item.discovery_candidate_id,
                source_item_id=item.source_item_id,
                clip_id=item.clip_id,
            )
            for item in preview.excluded
        ],
        ordered_clip_ids=list(preview.ordered_clip_ids),
    )


@router.get(
    "/channels/{channel_profile_id}/trends/opportunities/{opportunity_id}/activation",
    response_model=TrendActivationPreviewResponse,
)
def get_trend_activation_preview(
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    item_count: int | None = Query(default=None, ge=1, le=20),
) -> TrendActivationPreviewResponse:
    try:
        return _preview_response(
            preview_trend_activation(
                channel_profile_id,
                opportunity_id,
                item_count=item_count,
            )
        )
    except (KeyError, ValueError) as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc


@router.post(
    "/channels/{channel_profile_id}/trends/opportunities/{opportunity_id}/activate",
    response_model=ActivateTrendOpportunityResponse,
    status_code=status.HTTP_201_CREATED,
)
def activate_trend_episode(
    channel_profile_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    request: ActivateTrendOpportunityRequest,
) -> ActivateTrendOpportunityResponse:
    try:
        episode, preview = activate_trend_opportunity(
            channel_profile_id,
            opportunity_id,
            item_count=request.item_count,
            actor=request.actor,
        )
    except (KeyError, ValueError) as exc:
        message = str(exc)
        code = 404 if "not found" in message else 409
        raise HTTPException(status_code=code, detail=message) from exc

    return ActivateTrendOpportunityResponse(
        short_episode_id=episode.id,
        status=episode.status,
        preview=_preview_response(preview),
    )
