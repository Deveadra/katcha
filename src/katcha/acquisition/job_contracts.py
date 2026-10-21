"""Bounded remote-worker messages. Worker results remain unverified evidence."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal[
    "queued",
    "leased",
    "awaiting_validation",
    "failed",
    "cancelled",
    "recovery_required",
    "completed",
]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EnqueueJob(Message):
    receipt_id: uuid.UUID
    max_bytes: int = Field(default=4 * 1024**3, ge=1, le=64 * 1024**3)
    media_type: Literal["video", "image", "audio"] = "video"


class ClaimJob(Message):
    # Persist this identity locally before requesting a lease; reuse on HTTP retry.
    claim_id: uuid.UUID


class LeaseMessage(Message):
    generation: int = Field(ge=1)
    claim_id: uuid.UUID


class HeartbeatJob(LeaseMessage):
    sequence: int = Field(ge=1, le=2**31 - 1)
    bytes_received: int = Field(ge=0, le=64 * 1024**3)
    phase: Literal["acquiring", "uploading", "awaiting_operator"]


class ReportJob(LeaseMessage):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1, le=64 * 1024**3)


class ReleaseSource(Message):
    acknowledge_unknown_outcome: Literal[True]


class UploadPart(LeaseMessage):
    content_md5: str = Field(min_length=24, max_length=24)


class TransferView(Message):
    part_bytes: int
    size_bytes: int
    sha256: str
    uploaded_parts: list[int]
    completed: bool


class PartView(Message):
    url: str | None
    headers: dict[str, str] = Field(default_factory=dict)
    uploaded: bool = False


class FailJob(LeaseMessage):
    code: Literal[
        "authentication_required",
        "access_denied",
        "rate_limited",
        "unsupported_source",
        "invalid_media",
        "size_limit",
        "transport_error",
        "session_lost",
        "outcome_unknown",
    ]


class JobView(Message):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    schema_version: Literal[1] = 1
    id: uuid.UUID
    receipt_id: uuid.UUID
    channel_profile_id: uuid.UUID
    status: JobStatus
    generation: int
    max_bytes: int
    media_type: str = "video"
    source_item_id: uuid.UUID | None = None
    bytes_received: int
    heartbeat_sequence: int
    phase: str
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    # No remote claim can assert provider feasibility or validated media.
    media_verified: bool = False


class LeaseView(Message):
    job: JobView
    claim_id: uuid.UUID
    fetch_url: str
    staging_key: str
    permission: dict
