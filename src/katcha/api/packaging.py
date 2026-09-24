from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.orchestration.client import start_packaging_activation_workflow
from katcha.packaging_models import (
    PackagingCandidateGeneration,
    PackagingExperiment,
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.services.packaging import (
    create_packaging_variant,
    list_packaging_activations,
    list_packaging_variants,
    register_packaging_activation,
)
from katcha.services.packaging_experiments import (
    experiment_eligibility,
    list_packaging_experiments,
    reconcile_packaging_experiment,
    start_packaging_experiment,
)
from katcha.services.packaging_generation import (
    AmbiguousPackagingGeneration,
    PackagingGenerationUnavailable,
    generate_packaging_candidates,
    list_packaging_generations,
)
from katcha.services.packaging_thumbnails import build_packaging_thumbnail

router = APIRouter(prefix="/v1/publications", tags=["packaging"])


class CreatePackagingVariantRequest(BaseModel):
    variant_key: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=100)
    description: str = ""
    thumbnail_storage_key: str | None = None
    created_by: str = Field(default="operator", min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PackagingVariantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    publication_id: uuid.UUID
    variant_key: str
    version: int
    title: str
    description: str
    thumbnail_storage_key: str | None
    thumbnail_content_type: str | None
    thumbnail_size_bytes: int | None
    thumbnail_sha256: str | None
    created_by: str
    variant_metadata: dict[str, Any]
    created_at: datetime


class ActivatePackagingRequest(BaseModel):
    variant_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=160)


class PackagingActivationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    publication_id: uuid.UUID
    variant_id: uuid.UUID
    activation_key: str
    workflow_id: str
    status: str
    stage: str
    youtube_video_id: str
    error: str | None
    activation_metadata: dict[str, Any]
    created_at: datetime
    applied_at: datetime | None



class GeneratePackagingCandidatesRequest(BaseModel):
    generation_key: str = Field(min_length=1, max_length=160)
    candidate_count: int = Field(default=3, ge=2, le=5)


class PackagingGenerationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    publication_id: uuid.UUID
    generation_key: str
    status: str
    stage: str
    prompt_version: str
    context_sha256: str | None
    provider: str | None
    model: str | None
    candidate_payload: list[dict[str, Any]]
    accepted_variant_ids: list[str]
    generation_metadata: dict[str, Any]
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class PackagingGenerationDetailResponse(BaseModel):
    generation: PackagingGenerationResponse
    variants: list[PackagingVariantResponse]



class BuildThumbnailRequest(BaseModel):
    parent_variant_id: uuid.UUID


class BuildThumbnailResponse(BaseModel):
    parent_variant: PackagingVariantResponse
    thumbnail_variant: PackagingVariantResponse



class PackagingExperimentEligibilityResponse(BaseModel):
    eligible: bool
    reasons: list[str]
    publication_id: uuid.UUID
    baseline_variant_id: uuid.UUID | None
    candidate_variant_id: uuid.UUID | None
    intelligence_snapshot_id: uuid.UUID | None
    automation_version: int | None
    maturity_days: int | None
    recommendation: dict[str, Any] | None


class PackagingExperimentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    publication_id: uuid.UUID
    experiment_key: str
    baseline_variant_id: uuid.UUID
    candidate_variant_id: uuid.UUID
    intelligence_snapshot_id: uuid.UUID
    automation_version: int
    start_activation_id: uuid.UUID | None
    rollback_activation_id: uuid.UUID | None
    status: str
    stage: str
    observe_after: datetime
    evidence_snapshot: dict[str, Any]
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class PackagingExperimentActionResponse(BaseModel):
    action: str
    reason: str
    experiment: PackagingExperimentResponse | None
    activation: PackagingActivationResponse | None


@router.post(
    "/{publication_id}/packaging/variants",
    response_model=PackagingVariantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_variant(
    publication_id: uuid.UUID,
    request: CreatePackagingVariantRequest,
) -> PublicationPackagingVariant:
    try:
        return create_packaging_variant(
            publication_id,
            variant_key=request.variant_key,
            version=request.version,
            title=request.title,
            description=request.description,
            thumbnail_storage_key=request.thumbnail_storage_key,
            created_by=request.created_by,
            metadata=request.metadata,
        )
    except ValueError as exc:
        code = 404 if "publication not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get(
    "/{publication_id}/packaging/variants",
    response_model=list[PackagingVariantResponse],
)
def get_variants(publication_id: uuid.UUID) -> list[PublicationPackagingVariant]:
    try:
        return list_packaging_variants(publication_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{publication_id}/packaging/generations",
    response_model=PackagingGenerationDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_candidates(
    publication_id: uuid.UUID,
    request: GeneratePackagingCandidatesRequest,
) -> PackagingGenerationDetailResponse:
    try:
        result = generate_packaging_candidates(
            publication_id,
            generation_key=request.generation_key,
            candidate_count=request.candidate_count,
        )
        return PackagingGenerationDetailResponse(
            generation=PackagingGenerationResponse.model_validate(result.generation),
            variants=[
                PackagingVariantResponse.model_validate(variant)
                for variant in result.variants
            ],
        )
    except AmbiguousPackagingGeneration as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackagingGenerationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        code = 404 if "publication not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get(
    "/{publication_id}/packaging/generations",
    response_model=list[PackagingGenerationResponse],
)
def get_generations(
    publication_id: uuid.UUID,
) -> list[PackagingCandidateGeneration]:
    try:
        return list_packaging_generations(publication_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{publication_id}/packaging/thumbnails",
    response_model=BuildThumbnailResponse,
    status_code=status.HTTP_201_CREATED,
)
def build_thumbnail(
    publication_id: uuid.UUID,
    request: BuildThumbnailRequest,
) -> BuildThumbnailResponse:
    try:
        result = build_packaging_thumbnail(
            publication_id,
            parent_variant_id=request.parent_variant_id,
        )
        return BuildThumbnailResponse(
            parent_variant=PackagingVariantResponse.model_validate(
                result.parent_variant
            ),
            thumbnail_variant=PackagingVariantResponse.model_validate(
                result.thumbnail_variant
            ),
        )
    except ValueError as exc:
        code = 404 if "publication not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/{publication_id}/packaging/experiment-eligibility",
    response_model=PackagingExperimentEligibilityResponse,
)
def get_experiment_eligibility(
    publication_id: uuid.UUID,
) -> PackagingExperimentEligibilityResponse:
    try:
        result = experiment_eligibility(publication_id)
        return PackagingExperimentEligibilityResponse(
            eligible=result.eligible,
            reasons=list(result.reasons),
            publication_id=result.publication_id,
            baseline_variant_id=result.baseline_variant_id,
            candidate_variant_id=result.candidate_variant_id,
            intelligence_snapshot_id=result.intelligence_snapshot_id,
            automation_version=result.automation_version,
            maturity_days=result.maturity_days,
            recommendation=result.recommendation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{publication_id}/packaging/experiments",
    response_model=PackagingExperimentActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_experiment(
    publication_id: uuid.UUID,
) -> PackagingExperimentActionResponse:
    try:
        result = start_packaging_experiment(publication_id)
    except ValueError as exc:
        code = 404 if "publication not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if result.activation is not None and result.activation.status in {"queued", "running"}:
        await start_packaging_activation_workflow(
            str(result.activation.id),
            result.activation.workflow_id,
        )
    return PackagingExperimentActionResponse(
        action=result.action,
        reason=result.reason,
        experiment=(
            PackagingExperimentResponse.model_validate(result.experiment)
            if result.experiment is not None
            else None
        ),
        activation=(
            PackagingActivationResponse.model_validate(result.activation)
            if result.activation is not None
            else None
        ),
    )


@router.get(
    "/{publication_id}/packaging/experiments",
    response_model=list[PackagingExperimentResponse],
)
def get_experiments(
    publication_id: uuid.UUID,
) -> list[PackagingExperiment]:
    try:
        return list_packaging_experiments(publication_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{publication_id}/packaging/experiments/{experiment_id}/reconcile",
    response_model=PackagingExperimentActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reconcile_experiment(
    publication_id: uuid.UUID,
    experiment_id: uuid.UUID,
) -> PackagingExperimentActionResponse:
    try:
        result = reconcile_packaging_experiment(experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.experiment is not None and result.experiment.publication_id != publication_id:
        raise HTTPException(status_code=404, detail="packaging experiment not found")
    if result.activation is not None and result.activation.status in {"queued", "running"}:
        await start_packaging_activation_workflow(
            str(result.activation.id),
            result.activation.workflow_id,
        )
    return PackagingExperimentActionResponse(
        action=result.action,
        reason=result.reason,
        experiment=(
            PackagingExperimentResponse.model_validate(result.experiment)
            if result.experiment is not None
            else None
        ),
        activation=(
            PackagingActivationResponse.model_validate(result.activation)
            if result.activation is not None
            else None
        ),
    )


@router.post(
    "/{publication_id}/packaging/activations",
    response_model=PackagingActivationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def activate_variant(
    publication_id: uuid.UUID,
    request: ActivatePackagingRequest,
) -> PublicationPackagingActivation:
    try:
        activation = register_packaging_activation(
            publication_id,
            variant_id=request.variant_id,
            activation_key=request.idempotency_key,
        )
    except ValueError as exc:
        code = 404 if "publication not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    if activation.status in {"queued", "running"}:
        await start_packaging_activation_workflow(
            str(activation.id),
            activation.workflow_id,
        )
    return activation


@router.get(
    "/{publication_id}/packaging/activations",
    response_model=list[PackagingActivationResponse],
)
def get_activations(
    publication_id: uuid.UUID,
) -> list[PublicationPackagingActivation]:
    try:
        return list_packaging_activations(publication_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
