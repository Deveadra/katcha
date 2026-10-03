"""Versioned draft contracts; structural validation is not factual/rights approval."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Identity = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_-]{1,80}$")
]
RequestKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EditorialBrief(Contract):
    prompt: Text
    source_urls: list[HttpUrl] = Field(min_length=1, max_length=20)
    target_duration_seconds: int = Field(default=420, ge=15, le=3600, strict=True)

    @model_validator(mode="after")
    def unique_urls(self) -> Self:
        if len(set(map(str, self.source_urls))) != len(self.source_urls):
            raise ValueError("source URLs must be unique")
        return self


class CreateEditorialProject(Contract):
    brief: EditorialBrief
    idempotency_key: RequestKey


class SourceObservation(Contract):
    id: Identity
    source_url: HttpUrl
    source_duration_seconds: float = Field(gt=0, le=86400)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    observation: Text
    coverage: Literal["native_video", "sampled_frames", "transcript_only", "manual"]

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if not self.start_seconds < self.end_seconds <= self.source_duration_seconds:
            raise ValueError("observation range must lie within the source duration")
        return self


class ResearchSource(Contract):
    id: Identity
    url: HttpUrl
    title: Text
    category: Literal["official", "interview", "trade", "reference", "community", "other"]
    retrieved_at: AwareDatetime
    published_at: AwareDatetime | None = None
    excerpt: Text
    locator: Text


class EditorialClaim(Contract):
    id: Identity
    text: Text
    classification: Literal["confirmed", "inference", "theory"]
    confidence: float = Field(ge=0, le=1)
    observation_ids: list[Identity] = Field(default_factory=list, max_length=30)
    source_ids: list[Identity] = Field(default_factory=list, max_length=30)
    verification: Literal["unverified", "supported", "disputed", "rejected"] = "unverified"
    verification_note: Text | None = None
    contradictions: list[Text] = Field(default_factory=list, max_length=20)
    follow_up_questions: list[Text] = Field(default_factory=list, max_length=20)
    audience_value: Text

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        if not self.source_ids and not self.observation_ids:
            raise ValueError("claims require source or observation evidence")
        if self.verification != "unverified" and not self.verification_note:
            raise ValueError("reviewed claims require a verification note")
        if self.classification == "confirmed" and self.contradictions:
            raise ValueError("unresolved contradictions cannot be classified as confirmed")
        return self


class ScriptBeat(Contract):
    id: Identity
    role: Literal["hook", "reveal", "transition", "context", "closing"]
    narration: Text
    claim_ids: list[Identity] = Field(default_factory=list, max_length=30)
    nonfactual: bool = False
    uncertainty_disclosure: Text | None = None
    planned_duration_seconds: float = Field(gt=0, le=600)
    visual_intent: Text


class EditorialDraft(Contract):
    version: Literal["editorial-draft-v1"] = "editorial-draft-v1"
    observations: list[SourceObservation] = Field(default_factory=list, max_length=200)
    sources: list[ResearchSource] = Field(default_factory=list, max_length=100)
    claims: list[EditorialClaim] = Field(default_factory=list, max_length=200)
    script: list[ScriptBeat] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > 1_000_000:
            raise ValueError("editorial draft exceeds the 1 MB snapshot limit")

        def identities(items: list) -> set[str]:
            result = {item.id for item in items}
            if len(result) != len(items):
                raise ValueError("duplicate IDs are not allowed within an artifact type")
            return result

        observations = identities(self.observations)
        sources = identities(self.sources)
        identities(self.claims)
        identities(self.script)
        claims = {claim.id: claim for claim in self.claims}
        for claim in self.claims:
            if not set(claim.source_ids) <= sources:
                raise ValueError(f"claim {claim.id} references an unknown research source")
            if not set(claim.observation_ids) <= observations:
                raise ValueError(f"claim {claim.id} references an unknown observation")
        for beat in self.script:
            if not beat.claim_ids and not beat.nonfactual:
                raise ValueError(f"script beat {beat.id} requires claim evidence")
            if beat.nonfactual and beat.claim_ids:
                raise ValueError("nonfactual beats cannot carry factual claims")
            if not set(beat.claim_ids) <= claims.keys():
                raise ValueError(f"script beat {beat.id} references an unknown claim")
            for claim_id in beat.claim_ids:
                claim = claims[claim_id]
                if claim.verification == "rejected":
                    raise ValueError("rejected claims cannot enter a script")
                if claim.classification == "confirmed" and claim.verification != "supported":
                    raise ValueError("confirmed script claims need supporting verification")
                uncertain = claim.classification != "confirmed" or claim.verification != "supported"
                if uncertain and not beat.uncertainty_disclosure:
                    raise ValueError("uncertain script claims require an explicit disclosure")
        return self


class SaveEditorialDraft(Contract):
    expected_revision: int = Field(ge=0, strict=True)
    idempotency_key: RequestKey
    draft: EditorialDraft


class EditorialProjectResponse(Contract):
    id: str
    channel_profile_id: str
    brief: EditorialBrief
    revision: int
    status: Literal["draft"] = "draft"
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "draft_storage",
            "evidence_validation",
            "source_intake",
            "research",
            "script_generation",
            "asset_scout",
            "asset_review_acquisition",
        ]
    )
    limitation: str = (
        "Research/script runs require an eligible live provider. Coverage is recorded as "
        "native video or sampled frames. Generated claims require editorial review. "
        "Asset clearance, rendering and publication are not connected yet."
    )
    created_at: datetime
    updated_at: datetime


class EditorialRevisionResponse(Contract):
    revision: int
    digest: str
    draft: EditorialDraft
    created_at: datetime
    actor: str
