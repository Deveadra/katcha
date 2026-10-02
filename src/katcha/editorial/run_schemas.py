from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from katcha.editorial.project_schemas import Contract, RequestKey


class StartEditorialRun(Contract):
    idempotency_key: RequestKey
    expected_revision: int = Field(ge=0, strict=True)
    # This contract grows only as its actual workers are implemented.
    target: Literal["analysis"] = "analysis"
    clip_bindings: dict[str, uuid.UUID] = Field(default_factory=dict, max_length=20)


class ResumeEditorialRun(Contract):
    expected_attempt: int = Field(ge=1, strict=True)
