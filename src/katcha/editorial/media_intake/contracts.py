"""Versioned contracts for provider permissions and acquisition evidence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from katcha.editorial.project_schemas import Contract


class ProviderPermissionV1(Contract):
    provider: str = Field(min_length=1, max_length=80)
    operations: set[Literal["metadata", "preview", "download"]]
    evidence_scope: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    expires_at: datetime | None = None

    def allows(self, operation: str, *, now: datetime | None = None) -> bool:
        current = now or datetime.now().astimezone()
        if self.expires_at is not None:
            expiry = self.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=current.tzinfo)
            if expiry <= current:
                return False
        return operation in self.operations


class AcquisitionReceiptV1(Contract):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, from_attributes=True)
    id: uuid.UUID
    candidate_id: uuid.UUID
    operation_key: str
    authorization_scope: dict[str, object]
    fetch_url_if_allowed: str | None = None
    status: Literal["authorized", "acquiring", "succeeded", "failed", "unknown"]
    request_time: datetime
    response_time: datetime | None = None
    bytes_received: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    mime: str | None = None
    media_probe: dict[str, object] = Field(default_factory=dict)
    object_store_key: str | None = None
    duplicate_of: uuid.UUID | None = None
    source_version: str | None = None
    download_terms: dict[str, object] = Field(default_factory=dict)
    cost_receipt: dict[str, object] = Field(default_factory=dict)
    error_category: str | None = None
    safe_resume_cursor: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> AcquisitionReceiptV1:
        if self.status == "succeeded" and (self.sha256 is None or self.object_store_key is None):
            raise ValueError("A succeeded acquisition requires a content hash and object key")
        if self.status == "unknown" and not self.error_category:
            raise ValueError("An unknown provider outcome requires an error category")
        return self


def production_eligibility(rights_state: str, permission: ProviderPermissionV1) -> tuple[bool, str]:
    if rights_state != "production_cleared":
        return False, f"Rights state {rights_state!r} does not authorize production use"
    if not permission.allows("download"):
        return False, "Provider download permission is absent or expired"
    return True, "Current rights evidence and provider download permission are present"
