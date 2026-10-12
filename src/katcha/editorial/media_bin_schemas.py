"""HTTP contracts for project Media Bin references and status inspection."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, HttpUrl, model_validator

from katcha.editorial.project_schemas import Contract, Identity


class AddProjectMediaItemRequest(Contract):
    revision: int = Field(ge=1)
    kind: Literal["video", "still", "audio", "graphic", "citation", "unknown"]
    clip_id: uuid.UUID | None = None
    external_reference_url: str | None = Field(default=None, min_length=1, max_length=4096)
    requirement_ids: list[Identity] = Field(default_factory=list, max_length=100)
    searchable_tags: list[Identity] = Field(default_factory=list, max_length=100)
    source_in_seconds: float | None = Field(default=None, ge=0)
    source_out_seconds: float | None = Field(default=None, gt=0)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if (self.clip_id is None) == (self.external_reference_url is None):
            raise ValueError("Choose an existing clip or a source link")
        if (
            self.source_in_seconds is not None
            and self.source_out_seconds is not None
            and self.source_out_seconds <= self.source_in_seconds
        ):
            raise ValueError("Source out time must follow source in time")
        return self


class ProjectMediaItemResponse(Contract):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    revision: int
    channel_profile_id: uuid.UUID
    clip_id: uuid.UUID | None
    external_reference_url: HttpUrl | None
    kind: str
    status: Literal["reference_only", "review_required", "denied", "acquired", "ready"]
    rights_status: Literal[
        "not_assessed",
        "reference_only",
        "review_permitted",
        "production_cleared",
        "restricted",
        "denied",
        "revoked",
        "expired",
    ]
    rights_assessment_id: uuid.UUID | None
    rights_assessment_version: int | None
    requirement_ids: list[str]
    searchable_tags: list[str]
    provenance: dict = Field(default_factory=dict)
    source_metadata: dict = Field(default_factory=dict)
    source_in_seconds: float | None
    source_out_seconds: float | None
    notes: str
    created_at: datetime
    last_verified_at: datetime | None
