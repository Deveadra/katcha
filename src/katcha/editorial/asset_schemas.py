"""Semantic asset discovery contracts. Model output never grants media rights."""

from typing import Literal, Self

from pydantic import Field, HttpUrl, model_validator

from katcha.editorial.project_schemas import Contract, Identity, Text


class AssetRequest(Contract):
    id: Identity
    beat_id: Identity
    claim_ids: list[Identity] = Field(default_factory=list, max_length=30)
    purpose: Text
    query: Text
    medium: Literal["video", "image", "quote", "diagram"]
    fallback: Literal["source_frame", "quote_card", "original_diagram", "manual"]


class AssetPlan(Contract):
    requests: list[AssetRequest] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_requests(self) -> Self:
        if len({request.id for request in self.requests}) != len(self.requests):
            raise ValueError("Asset request IDs must be unique")
        return self


class AssetLead(Contract):
    url: HttpUrl
    title: Text
    medium: Literal["video", "image", "reference_page"]
    relevance: Text
    credit_hint: Text | None = None


class AssetLeads(Contract):
    leads: list[AssetLead] = Field(default_factory=list, max_length=5)
