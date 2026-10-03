from __future__ import annotations

from typing import Literal

from pydantic import Field, HttpUrl

from katcha.editorial.project_schemas import (
    Contract,
    EditorialClaim,
    ScriptBeat,
    SourceObservation,
    Text,
)


class Observations(Contract):
    observations: list[SourceObservation] = Field(default_factory=list, max_length=40)
    limitations: list[Text] = Field(default_factory=list, max_length=10)


class ResearchQuestion(Contract):
    question: Text
    specialty: Literal["official", "background", "community", "verification"]
    reason: Text


class ResearchPlan(Contract):
    questions: list[ResearchQuestion] = Field(min_length=1, max_length=12)


class SearchLead(Contract):
    url: HttpUrl
    title: Text


class ResearchLeads(Contract):
    leads: list[SearchLead] = Field(default_factory=list, max_length=5)


class SourceExcerpt(Contract):
    source_id: str = Field(min_length=1, max_length=80)
    excerpt: str = Field(min_length=1, max_length=2000)
    category: Literal["official", "interview", "trade", "reference", "community", "other"]


class ResearchFindings(Contract):
    claims: list[EditorialClaim] = Field(default_factory=list, max_length=40)
    follow_ups: list[ResearchQuestion] = Field(default_factory=list, max_length=5)
    excerpts: list[SourceExcerpt] = Field(default_factory=list, max_length=5)


class ClaimReview(Contract):
    claim_id: str = Field(min_length=1, max_length=80)
    verdict: Literal["supported", "disputed", "rejected"]
    rationale: Text
    contradictions: list[Text] = Field(default_factory=list, max_length=10)


class EvidenceReview(Contract):
    reviews: list[ClaimReview] = Field(default_factory=list, max_length=200)


class EditorialScript(Contract):
    title: Text
    beats: list[ScriptBeat] = Field(min_length=1, max_length=100)
    selection_rationale: Text


class EditorialCritique(Contract):
    passed: bool
    issues: list[Text] = Field(default_factory=list, max_length=20)
