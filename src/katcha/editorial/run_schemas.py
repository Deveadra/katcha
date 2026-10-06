from __future__ import annotations

import uuid
from typing import Literal, Self

from pydantic import Field, model_validator

from katcha.editorial.project_schemas import Contract, Identity, RequestKey
from katcha.editorial.visual_schemas import DirectionOptions, StoryboardPlan


class StartEditorialRun(Contract):
    idempotency_key: RequestKey
    expected_revision: int = Field(ge=0, strict=True)
    # This contract grows only as its actual workers are implemented.
    target: Literal[
        "analysis", "script", "assets", "acquire_assets", "render", "narration", "direction"
    ] = "analysis"
    confirm_narration: bool = False
    max_narration_estimate_usd: float = Field(default=0.5, gt=0, le=25, allow_inf_nan=False)
    asset_run_id: uuid.UUID | None = None
    direction_run_id: uuid.UUID | None = None
    storyboard: StoryboardPlan | None = None
    direction: DirectionOptions | None = None
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
        if self.direction_run_id is not None and self.target != "render":
            raise ValueError("A saved visual plan may only be selected for rendering")
        if self.target == "narration" and not self.confirm_narration:
            raise ValueError("Confirm use of the configured channel voice and budget")
        if self.target != "narration" and self.confirm_narration:
            raise ValueError("Narration confirmation is only valid for narration generation")
        if self.target == "acquire_assets":
            if not self.scout_run_id or not self.asset_candidate_ids:
                raise ValueError("Asset acquisition requires a scout run and selected candidates")
            if len(set(self.asset_candidate_ids)) != len(self.asset_candidate_ids):
                raise ValueError("Asset candidate selection must be unique")
        elif self.scout_run_id or self.asset_candidate_ids:
            raise ValueError("Asset selection is only valid for asset acquisition")
        if self.target == "render":
            if self.storyboard is None or (
                any(beat.media for beat in self.storyboard.beats) and not self.asset_run_id
            ):
                raise ValueError("Rendering requires acquired assets and an explicit storyboard")
        elif self.target == "direction":
            if not self.asset_run_id or self.direction is None or self.storyboard is not None:
                raise ValueError(
                    "Visual direction requires acquired assets and presentation options"
                )
        elif self.asset_run_id or self.storyboard is not None:
            raise ValueError("Storyboard selection is only valid for rendering")
        if self.target != "direction" and self.direction is not None:
            raise ValueError("Direction options are only valid for visual direction")
        return self


class ResumeEditorialRun(Contract):
    expected_attempt: int = Field(ge=1, strict=True)


class ReconcileNarrationBilling(Contract):
    idempotency_key: RequestKey
    beat_id: Identity
    expected_dispatch_count: int = Field(ge=1, strict=True)
    outcome: Literal["charged", "not_charged"]
    actual_cost_usd: float = Field(ge=0, le=1000, allow_inf_nan=False)
    provider_receipt: str = Field(min_length=1, max_length=2000)
    confirmed: bool

    @model_validator(mode="after")
    def confirmed_charge(self) -> Self:
        if not self.confirmed or not self.provider_receipt.strip():
            raise ValueError("Confirm the provider's final outcome and include its receipt")
        if self.outcome == "not_charged" and self.actual_cost_usd != 0:
            raise ValueError("An uncharged request must have zero actual cost")
        return self
