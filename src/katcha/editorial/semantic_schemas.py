"""Optional model hypotheses; source coordinates remain measured server authority."""

from __future__ import annotations

import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SemanticMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preparation_run_id: uuid.UUID
    expected_revision: int = Field(ge=1)
    media_item_id: uuid.UUID
    requirement_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    shot_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    idempotency_key: str = Field(min_length=1, max_length=120)
    external_inspection_authorized: bool = False

    @model_validator(mode="after")
    def unique_choices(self) -> Self:
        if len(set(self.requirement_ids)) != len(self.requirement_ids) or len(
            set(self.shot_ids)
        ) != len(self.shot_ids):
            raise ValueError("Choose each requirement and measured shot once")
        if any(not 0 < len(value) <= 160 for value in (*self.requirement_ids, *self.shot_ids)):
            raise ValueError("Unsupported requirement or shot identity")
        return self


class SemanticHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str = Field(min_length=1, max_length=160)
    shot_id: str = Field(min_length=1, max_length=160)
    visible_details_hypothesis: str = Field(min_length=1, max_length=600)
    match_explanation: str = Field(min_length=1, max_length=600)
    confidence: Literal["low", "medium"]
    uncertainties: tuple[str, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def bounded_uncertainty(self) -> Self:
        if any(not 0 < len(value) <= 300 for value in self.uncertainties):
            raise ValueError("Bounded uncertainty is required")
        return self


class SemanticModelResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypotheses: tuple[SemanticHypothesis, ...] = Field(default_factory=tuple, max_length=16)
    unresolved_requirement_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
