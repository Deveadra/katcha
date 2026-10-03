"""A review records an operator decision, never permission to publish."""

from typing import Literal

from pydantic import Field

from katcha.editorial.project_schemas import Contract


class ReviewEditorialRender(Contract):
    idempotency_key: str = Field(min_length=1, max_length=120)
    expected_revision: int = Field(gt=0, strict=True)
    expected_review_sequence: int = Field(ge=0, strict=True)
    decision: Literal["approve", "request_changes"]
    note: str = Field(default="", max_length=4000)
