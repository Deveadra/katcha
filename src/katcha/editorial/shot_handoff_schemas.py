"""Explicit operator handoff; requests carry IDs, never permission or storage claims."""

import uuid

from pydantic import Field

from katcha.editorial.project_schemas import Contract, Identity, RequestKey


class ApplyApprovedShot(Contract):
    choice_id: uuid.UUID
    expected_revision: int = Field(ge=1, strict=True)
    expected_storyboard_version: int = Field(ge=0, strict=True)
    beat_id: Identity
    idempotency_key: RequestKey
    replace_existing: bool = False
    freeze: bool = False
