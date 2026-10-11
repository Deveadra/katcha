"""Versioned search intent and receipts, independent of the script parser."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator

Text = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
Money = Annotated[Decimal, Field(ge=0, le=100, max_digits=12, decimal_places=6)]
Status = Literal[
    "planned",
    "running",
    "succeeded",
    "quota_exceeded",
    "policy_blocked",
    "rate_limited",
    "failed",
    "unknown",
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def bounded_cursor(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if len(canonical(value).encode("utf-8")) > 4096:
        raise ValueError("cursor exceeds 4096 bytes")

    def walk(item: JsonValue, depth: int) -> None:
        if depth > 4:
            raise ValueError("cursor exceeds maximum depth")
        if isinstance(item, dict):
            for key, child in item.items():
                if len(key) > 100 or any(
                    term in key.casefold()
                    for term in ("password", "secret", "api_key", "authorization")
                ):
                    raise ValueError("cursor contains an unsupported key")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)

    walk(value, 0)
    return value


class SearchScopeV1(Record):
    schema_version: Literal[1] = 1
    channel_id: UUID
    project_id: UUID
    revision_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @property
    def key(self) -> str:
        return digest(self.model_dump(mode="json"))


class SearchIntentV1(Record):
    schema_version: Literal[1] = 1
    scope: SearchScopeV1
    requirement_id: Text
    query_terms: Annotated[tuple[Text, ...], Field(min_length=1, max_length=30)]
    negative_terms: Annotated[tuple[Text, ...], Field(max_length=30)] = ()
    semantic_brief: Annotated[str, Field(min_length=1, max_length=4000)]
    source_purpose: Literal["exact_scene", "official_reference", "alternative", "citation"]
    requested_by: Text


class SourceSearchJobV1(SearchIntentV1):
    job_id: Annotated[str, Field(pattern=r"^ssj_[a-f0-9]{64}$")]
    idempotency_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    provider_key: Text
    provider_version: Text
    policy_version: Text
    cursor: dict[str, JsonValue] = Field(default_factory=dict)
    max_results: Annotated[int, Field(strict=True, ge=1, le=100)]
    max_cost: Money = Decimal("0")
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 1
    attempt: Literal[0] = 0
    status: Literal["planned"] = "planned"
    receipt_refs: tuple[str, ...] = ()

    _cursor = field_validator("cursor")(bounded_cursor)

    def identity_payload(self) -> dict[str, JsonValue]:
        return self.model_dump(
            mode="json",
            exclude={
                "job_id",
                "idempotency_key",
                "attempt",
                "status",
                "receipt_refs",
            },
        )

    def verify_identity(self) -> None:
        expected = digest(self.identity_payload())
        if self.idempotency_key != expected or self.job_id != "ssj_" + expected:
            raise ValueError("job identity does not match search intent")


class ProviderPolicyV1(Record):
    """Operator/scheduler supplied ceilings; never inferred by an AI worker.

    Monetary reservations use a conservative per-call ceiling. A paid legacy
    adapter without a transport-enforced ceiling remains blocked.
    """

    schema_version: Literal[1] = 1
    version: Text
    max_calls: Annotated[int, Field(strict=True, ge=1, le=1000)] = 10
    max_results: Annotated[int, Field(strict=True, ge=1, le=10000)] = 100
    max_cost: Money = Decimal("0")
    call_cost_ceiling: Money = Decimal("0")
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 1
    external_search_authorized: bool = False


class SearchReceiptV1(Record):
    schema_version: Literal[1] = 1
    receipt_id: str
    job_id: str
    scope: SearchScopeV1
    status: Status
    attempt: int
    started_at: str
    finished_at: str | None = None
    error_category: str | None = None
    next_action: str | None = None
    retry_after_seconds: int | None = None
    reserved_cost: Money = Decimal("0")
    provider_usage: dict[str, int] = Field(default_factory=dict)
    source_urls: tuple[str, ...] = ()
    next_cursor: dict[str, JsonValue] = Field(default_factory=dict)
    done: bool = True

    _cursor = field_validator("next_cursor")(bounded_cursor)
