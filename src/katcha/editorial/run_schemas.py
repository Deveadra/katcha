from __future__ import annotations

import uuid
from typing import Literal, Self

from pydantic import Field, model_validator

from katcha.editorial.project_schemas import Contract, Identity, RequestKey


class StartEditorialRun(Contract):
    idempotency_key: RequestKey
    expected_revision: int = Field(ge=0, strict=True)
    # This contract grows only as its actual workers are implemented.
    target: Literal["analysis", "script", "assets", "acquire_assets"] = "analysis"
    scout_run_id: uuid.UUID | None = None
    asset_candidate_ids: list[Identity] = Field(default_factory=list, max_length=30)
    clip_bindings: dict[str, uuid.UUID] = Field(default_factory=dict, max_length=20)
    max_queries: int = Field(default=12, ge=1, le=24, strict=True)
    max_documents: int = Field(default=20, ge=1, le=40, strict=True)
    max_depth: int = Field(default=2, ge=0, le=3, strict=True)
    max_model_calls: int = Field(default=30, ge=1, le=80, strict=True)
    max_model_tokens: int = Field(default=1_000_000, ge=1000, le=5_000_000, strict=True)
    max_elapsed_seconds: int = Field(default=7200, ge=60, le=7200, strict=True)

    @model_validator(mode="after")
    def valid_asset_selection(self) -> Self:
        if self.target == "acquire_assets":
            if not self.scout_run_id or not self.asset_candidate_ids:
                raise ValueError("Asset acquisition requires a scout run and selected candidates")
            if len(set(self.asset_candidate_ids)) != len(self.asset_candidate_ids):
                raise ValueError("Asset candidate selection must be unique")
        elif self.scout_run_id or self.asset_candidate_ids:
            raise ValueError("Asset selection is only valid for asset acquisition")
        return self


class ResumeEditorialRun(Contract):
    expected_attempt: int = Field(ge=1, strict=True)
