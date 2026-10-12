"""Persistence helpers that never turn an unknown provider outcome into a retry."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from katcha.acquisition_models import DiscoveryCandidate
from katcha.db import session_scope
from katcha.editorial.media_intake.contracts import (
    AcquisitionReceiptV1,
    ProviderPermissionV1,
)
from katcha.editorial_models import AcquisitionReceipt


def start_acquisition(
    channel_id: uuid.UUID,
    candidate_id: uuid.UUID,
    *,
    operation_key: str,
    permission: ProviderPermissionV1,
    fetch_url_if_allowed: str,
    actor: str,
) -> AcquisitionReceipt:
    """Create an authorized attempt or return its exact replay; unknown is terminal."""
    if not operation_key.strip():
        raise ValueError("A stable acquisition operation key is required")
    if not permission.allows("download"):
        raise ValueError("Provider-specific download permission is absent or expired")
    permission_snapshot = permission.model_dump(mode="json")
    permission_snapshot["operations"] = sorted(permission.operations)
    permission_snapshot["allowed_hosts"] = sorted(
        host.rstrip(".").casefold() for host in permission.allowed_hosts
    )
    payload = {
        "channel_id": str(channel_id),
        "candidate_id": str(candidate_id),
        "operation_key": operation_key,
        "permission": permission_snapshot,
        "fetch_url_if_allowed": fetch_url_if_allowed,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with session_scope() as session:
        candidate = session.get(DiscoveryCandidate, candidate_id)
        owner = (
            (candidate.candidate_metadata or {}).get("channel_profile_id")
            if candidate
            else None
        )
        if candidate is None or str(owner) != str(channel_id):
            raise ValueError("Discovery candidate is not available to this channel")
        if candidate.adapter_key != permission.provider:
            raise ValueError("Provider download permission does not match this source")
        row = session.scalar(
            select(AcquisitionReceipt).where(
                AcquisitionReceipt.channel_profile_id == channel_id,
                AcquisitionReceipt.operation_key == operation_key,
            )
        )
        if row is not None:
            if row.request_digest != digest:
                raise ValueError(
                    "Acquisition operation key was already used for a different request"
                )
            if row.status == "unknown":
                raise ValueError("Provider outcome is unknown; reconcile it before any retry")
            return row
        row = AcquisitionReceipt(
            id=uuid.uuid4(),
            channel_profile_id=channel_id,
            candidate_id=candidate_id,
            operation_key=operation_key,
            request_digest=digest,
            status="authorized",
            authorization_scope={
                "schema": "ProviderPermissionV1",
                "provider": permission.provider,
                "operations": sorted(permission.operations),
                "allowed_hosts": permission_snapshot["allowed_hosts"],
                "evidence_scope": permission.evidence_scope,
                "evidence_refs": permission.evidence_refs,
                "expires_at": permission.expires_at.isoformat() if permission.expires_at else None,
                "actor": actor,
            },
            fetch_url_if_allowed=fetch_url_if_allowed,
            request_time=datetime.now(UTC),
            media_probe={},
            download_terms={},
            cost_receipt={},
            safe_resume_cursor={},
        )
        session.add(row)
        session.flush()
        return row


def claim_acquisition(channel_id: uuid.UUID, receipt_id: uuid.UUID) -> AcquisitionReceipt:
    """Atomically claim an authorized receipt once; active/unknown attempts cannot replay."""
    with session_scope() as session:
        row = session.scalar(
            select(AcquisitionReceipt)
            .where(
                AcquisitionReceipt.id == receipt_id,
                AcquisitionReceipt.channel_profile_id == channel_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ValueError("Acquisition receipt not found in this channel")
        if row.status != "authorized":
            raise ValueError("Acquisition is unfinished or final; reconcile it before any retry")
        row.status = "acquiring"
        session.flush()
        return row


def receipt_contract(row: AcquisitionReceipt) -> AcquisitionReceiptV1:
    return AcquisitionReceiptV1.model_validate(row)


def record_outcome(
    channel_id: uuid.UUID,
    receipt_id: uuid.UUID,
    *,
    status: str,
    response_time: datetime,
    bytes_received: int | None = None,
    sha256: str | None = None,
    mime: str | None = None,
    media_probe: dict[str, Any] | None = None,
    object_store_key: str | None = None,
    error_category: str | None = None,
) -> AcquisitionReceipt:
    if status not in {"succeeded", "failed", "unknown"}:
        raise ValueError("Acquisition outcome must be succeeded, failed, or unknown")
    with session_scope() as session:
        row = session.scalar(
            select(AcquisitionReceipt).where(
                AcquisitionReceipt.id == receipt_id,
                AcquisitionReceipt.channel_profile_id == channel_id,
            )
        )
        if row is None:
            raise ValueError("Acquisition receipt not found in this channel")
        if row.status in {"unknown", "failed", "succeeded"}:
            raise ValueError("Acquisition outcome is already final and cannot be overwritten")
        if status == "succeeded" and (not sha256 or not object_store_key):
            raise ValueError("A succeeded acquisition requires a content hash and object key")
        if status == "unknown" and not error_category:
            raise ValueError("An unknown provider outcome requires an error category")
        row.status = status
        row.response_time = response_time
        row.bytes_received = bytes_received
        row.sha256 = sha256
        row.mime = mime
        row.media_probe = media_probe or {}
        row.object_store_key = object_store_key
        row.error_category = error_category
        session.flush()
        return row
