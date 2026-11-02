"""Explicit operator admission for Jockey asset indexing and semantic retrieval."""

from __future__ import annotations

import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JockeyAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    external_inspection_authorized: bool = False
    provider_spend_approved: bool = False


class JockeySearchRequest(JockeyAdmission):
    preparation_run_id: uuid.UUID
    requirement_id: str = Field(min_length=1, max_length=160)
    item_ids: tuple[uuid.UUID, ...] = Field(min_length=1, max_length=8)
    idempotency_key: str = Field(min_length=1, max_length=120)
    max_results: int = Field(default=8, ge=1, le=16)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("Select each media item only once")
        return self
