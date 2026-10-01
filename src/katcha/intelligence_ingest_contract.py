from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class IntelligenceRecordInputRequest(BaseModel):
    record_kind: str = Field(min_length=1, max_length=64)
    record_key: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=2000)
    summary: str | None = Field(default=None, max_length=8000)
    source_url: str | None = Field(default=None, max_length=4000)
    platform: str | None = Field(default=None, max_length=32)
    status: str = Field(default="active", min_length=1, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=50)
    payload: dict[str, object] = Field(default_factory=dict)
    provenance: dict[str, object] = Field(default_factory=dict)
    observed_at: datetime | None = None
    event_time: datetime | None = None


class IngestIntelligenceBatchRequest(BaseModel):
    channel_profile_id: uuid.UUID
    batch_key: str = Field(min_length=1, max_length=160)
    producer: str = Field(default="orion", min_length=1, max_length=128)
    source_type: str = Field(default="assistant", min_length=1, max_length=64)
    batch_metadata: dict[str, object] = Field(default_factory=dict)
    records: list[IntelligenceRecordInputRequest] = Field(min_length=1, max_length=500)
