from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from katcha.orchestration.client import start_packaging_activation_workflow
from katcha.packaging_models import (
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.services.packaging import (
    create_packaging_variant,
    list_packaging_activations,
    list_packaging_variants,
    register_packaging_activation,
)

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
