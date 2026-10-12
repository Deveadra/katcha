"""Bounded proposal contracts: measured evidence never proves semantic intent or rights."""

from __future__ import annotations

import math
import uuid
from typing import Annotated, Literal

from pydantic import Field, model_validator

from katcha.editorial.source_intelligence.contracts import Record, SearchScopeV1, Text
from katcha.editorial.source_intelligence.requirements import Hash


class MatchRequirementsRequest(Record):
    preparation_run_id: uuid.UUID
    expected_revision: Annotated[int, Field(ge=1)]
    item_ids: Annotated[tuple[uuid.UUID, ...], Field(min_length=1, max_length=8)]
    idempotency_key: Text

    @model_validator(mode="after")
    def unique_items(self) -> MatchRequirementsRequest:
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("Choose each media item once")
        return self


class RequirementBindingV1(Record):
    schema_version: Literal["RequirementBindingV1"] = "RequirementBindingV1"
    binding_id: Text
    scope: SearchScopeV1
    requirement_id: Text
    beat_id: Text | None
    media_item_id: uuid.UUID
    clip_id: uuid.UUID
    clip_sha256: Hash
    index_id: uuid.UUID
    shot_id: Text
    source_origin_tick: int
    duration_ticks: Annotated[int, Field(gt=0)]
    coverage: Literal["all_decoded_frames_visual_change_sparse_keyframes"]
    index_policy_version: Text
    visual_tags: Annotated[tuple[Text, ...], Field(max_length=8)]
    start_tick: Annotated[int, Field(ge=0)]
    end_tick: Annotated[int, Field(gt=0)]
    keyframe_tick: Annotated[int, Field(ge=0)]
    timebase_numerator: Annotated[int, Field(gt=0)]
    timebase_denominator: Annotated[int, Field(gt=0)]
    source_in_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    source_out_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    keyframe_sha256: Hash
    evidence_refs: Annotated[tuple[Text, ...], Field(max_length=128)]
    rank_score: Annotated[int, Field(ge=0, le=3)]
    match_basis: Literal["measured_visual_tokens", "requirement_link_fallback"]
    match_confidence: Literal["limited_visual_only", "unverified_semantics"]
    confidence_limits: Annotated[tuple[Text, ...], Field(max_length=20)]
    explanation: Annotated[str, Field(max_length=2000)]
    rights_assessment_id: uuid.UUID | None
    rights_assessment_version: int | None
    production_eligible: bool
    rights_granted: Literal[False] = False
    state: Literal["proposed"] = "proposed"
    current_status: Literal[
        "current", "stale_revision", "stale_source_or_permission", "requires_revalidation"
    ] = "current"

    @model_validator(mode="after")
    def normalized_measured_interval(self) -> RequirementBindingV1:
        if not 0 <= self.start_tick <= self.keyframe_tick < self.end_tick <= self.duration_ticks:
            raise ValueError("Proposal interval must stay within measured source bounds")
        scale = self.timebase_numerator / self.timebase_denominator
        if not (
            math.isclose(self.source_in_seconds, self.start_tick * scale, abs_tol=1e-9)
            and math.isclose(self.source_out_seconds, self.end_tick * scale, abs_tol=1e-9)
        ):
            raise ValueError("Proposal seconds must match measured source-relative ticks")
        if self.production_eligible and self.current_status != "current":
            raise ValueError("Stale proposals cannot remain production eligible")
        if self.production_eligible and (
            self.rights_assessment_id is None or not self.rights_assessment_version
        ):
            raise ValueError("Production eligibility needs matching current rights evidence")
        return self


class RequirementShotResultV1(Record):
    requirement_id: Text
    beat_id: Text | None
    proposals: Annotated[tuple[RequirementBindingV1, ...], Field(max_length=5)]
    unresolved_gaps: Annotated[tuple[Text, ...], Field(max_length=20)]


class ShotProposalPackageV1(Record):
    schema_version: Literal["ShotProposalPackageV1"] = "ShotProposalPackageV1"
    scope: SearchScopeV1
    item_ids: Annotated[tuple[uuid.UUID, ...], Field(min_length=1, max_length=8)]
    input_revision: int
    preparation_run_id: uuid.UUID
    policy_version: Literal["requirement-shot-v1"] = "requirement-shot-v1"
    requirements: Annotated[tuple[RequirementShotResultV1, ...], Field(max_length=12000)]
    item_gaps: dict[str, Text]
    provider_calls: Literal[0] = 0


class SelectShotRequest(Record):
    package_id: uuid.UUID
    binding_id: Text
    expected_revision: Annotated[int, Field(ge=1)]
    expected_choice_version: Annotated[int, Field(ge=0)] = 0
    idempotency_key: Text
    locked: bool = True
    note: Annotated[str, Field(max_length=2000)] = ""
