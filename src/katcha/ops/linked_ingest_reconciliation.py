"""Operator-approved reconciliation of a *linked, verified* held ingest.

No workflow is started and no R2 object is mutated. An unlinked raw object
cannot be attributed safely to a SourceItem by filename alone and remains
excluded from this narrowly scoped recovery transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import ClipStatus, SourceStatus
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.ops.ingest_recovery_evidence import _display_workflow_id, inspect_held_ingest

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_TEMPORAL = {"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
_ALLOWED_CLIP_STATUSES = {status.value for status in ClipStatus} - {ClipStatus.FAILED.value}
_APPROVAL_KIND = "linked_verified_clip_v1"


class RecoveryApprovalError(RuntimeError):
    """A mismatch or incomplete observation blocks the operator transaction."""


def _db_snapshot(source_id: uuid.UUID, *, lock: bool = False) -> dict[str, Any] | None:
    with session_scope() as session:
        statement = select(SourceItem).where(SourceItem.id == source_id)
        if lock:
            statement = statement.with_for_update()
        source = session.scalar(statement)
        if source is None:
            return None
        clip = session.get(Clip, source.clip_id) if source.clip_id else None
        return _fields(source, clip)


def _fields(source: SourceItem, clip: Clip | None) -> dict[str, Any]:
    return {
        "source_id": str(source.id),
        "source_status": source.status,
        "source_metadata_digest": hashlib.sha256(
            json.dumps(source.source_metadata or {}, sort_keys=True).encode()
        ).hexdigest(),
        "source_updated_at": source.updated_at.isoformat() if source.updated_at else None,
        "workflow_id": source.workflow_id,
        "linked_clip_id": str(source.clip_id) if source.clip_id else None,
        "remote_acquisition_job_recorded": bool(
            (source.source_metadata or {}).get("remote_acquisition_job_id")
        ),
        "clip_record_found": clip is not None,
        "clip": {
            "id": str(clip.id),
            "sha256": clip.sha256,
            "size_bytes": clip.size_bytes,
            "storage_key": clip.storage_key,
            "status": clip.status,
            "extension": clip.extension,
            "media_metadata_digest": hashlib.sha256(
                json.dumps(clip.media_metadata or {}, sort_keys=True).encode()
            ).hexdigest(),
            "has_video_metadata": any(
                isinstance(stream, dict) and stream.get("codec_type") == "video"
                for stream in ((clip.media_metadata or {}).get("streams") or [])
            ),
            "updated_at": clip.updated_at.isoformat() if clip.updated_at else None,
        } if clip else None,
    }


def _fingerprint(snapshot: dict[str, Any], report: dict[str, Any]) -> str:
    # Bound this token to source row, linked Clip identity + stored hash, the
    # *specific* Temporal run and result, and a successful bytewise R2 check.
    claims = {
        "kind": _APPROVAL_KIND,
        "source": snapshot,
        "temporal": report["temporal"],
        "clip_storage": report["linked_clip_storage"],
        "stage_complete": report["direct_staging"]["complete"],
        "staging_objects": report["direct_staging"]["completed_objects"],
        "staging_uploads": report["direct_staging"]["incomplete_uploads"],
    }
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def plan_linked_clip_recovery(
    source_id: uuid.UUID, *, temporal_client: Any = None,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    """Full bounded evidence check; never creates an approval by itself."""
    report = await inspect_held_ingest(
        source_id, temporal_client=temporal_client, store=store,
        max_records=2000, verify_sha256=True,
    )
    snapshot = _db_snapshot(source_id)
    reasons: list[str] = []
    if snapshot is None:
        reasons.append("source_missing")
    else:
        clip = snapshot["clip"]
        if snapshot["source_status"] != SourceStatus.INGESTING.value:
            reasons.append("source_not_ingesting")
        if snapshot["remote_acquisition_job_recorded"]:
            reasons.append("remote_acquisition_separate")
        if not snapshot["workflow_id"]:
            reasons.append("workflow_identity_missing")
        elif _display_workflow_id(snapshot["workflow_id"]) == "<redacted>":
            reasons.append("workflow_identity_untrusted")
        if clip is None:
            reasons.append("linked_clip_missing")
        else:
            if clip["status"] not in _ALLOWED_CLIP_STATUSES:
                reasons.append("clip_status_not_eligible")
            if not clip["has_video_metadata"]:
                reasons.append("clip_video_metadata_missing")
            if not isinstance(clip["sha256"], str) or not _SHA256.fullmatch(clip["sha256"]):
                reasons.append("clip_sha_invalid")
            elif clip["storage_key"] != ObjectStore.raw_key(
                clip["sha256"], str(clip.get("extension") or "")
            ):
                reasons.append("clip_object_key_not_content_addressed")
            if not isinstance(clip["size_bytes"], int) or clip["size_bytes"] <= 0:
                reasons.append("clip_size_invalid")
        if (
            snapshot["source_status"] != report.get("source_status")
            or snapshot["workflow_id"] != report.get("workflow_id")
            or snapshot["linked_clip_id"] != report.get("linked_clip_id")
            or report.get("source_changed_during_inspection")
        ):
            reasons.append("source_changed_during_inspection")
    temporal = report.get("temporal") or {}
    if (
        temporal.get("visibility") != "verified"
        or temporal.get("status") not in _TERMINAL_TEMPORAL
        or not temporal.get("run_id")
    ):
        reasons.append("temporal_terminal_run_unverified")
    stage = report.get("direct_staging") or {}
    if stage.get("visibility") != "verified" or not stage.get("complete"):
        reasons.append("stage_inventory_incomplete")
    if stage.get("completed_objects") or stage.get("incomplete_uploads"):
        reasons.append("staging_requires_separate_reconciliation")
    stored = report.get("linked_clip_storage") or {}
    if stored.get("state") != "head_matches_record":
        reasons.append("clip_storage_size_not_verified")
    if stored.get("sha256_check") != "verified":
        reasons.append("clip_bytes_not_verified")

    eligible = not reasons
    return {
        "source_id": str(source_id),
        "workflow_id": _display_workflow_id(snapshot["workflow_id"]) if snapshot else None,
        "linked_clip_id": snapshot["linked_clip_id"] if snapshot else None,
        "clip_sha256": snapshot["clip"]["sha256"] if snapshot and snapshot["clip"] else None,
        "temporal": temporal,
        "eligible": eligible,
        "blockers": sorted(set(reasons)),
        "approval_fingerprint": _fingerprint(snapshot, report) if eligible else None,
        "source_snapshot": snapshot if eligible else None,
        "retry_authorized": False,
        "proposed_action": "mark_existing_verified_clip_ready" if eligible else "no_action",
    }


async def finalize_linked_clip_recovery(
    source_id: uuid.UUID, *, approved_fingerprint: str,
    temporal_client: Any = None, store: ObjectStore | None = None,
) -> dict[str, Any]:
    """Explicit evidence-bound commit; repeated or stale approvals are rejected."""
    if not _SHA256.fullmatch(approved_fingerprint):
        raise RecoveryApprovalError("An exact 64-character approval fingerprint is required")
    plan = await plan_linked_clip_recovery(
        source_id, temporal_client=temporal_client, store=store,
    )
    if not plan["eligible"] or approved_fingerprint != plan["approval_fingerprint"]:
        raise RecoveryApprovalError("Recovery evidence is stale, missing, or ineligible")
    expected = plan["source_snapshot"]
    assert expected is not None
    with session_scope() as session:
        source = session.scalar(
            select(SourceItem).where(SourceItem.id == source_id).with_for_update()
        )
        if source is None:
            raise RecoveryApprovalError("Source disappeared before recovery commit")
        clip = session.get(Clip, source.clip_id) if source.clip_id else None
        # A linked/source/media change after verification invalidates approval.
        if _fields(source, clip) != expected:
            raise RecoveryApprovalError("Source or Clip changed before recovery commit")
        if clip is None:
            raise RecoveryApprovalError("Clip disappeared before recovery commit")
        source.status = SourceStatus.READY.value
        source.error = None
        metadata = dict(source.source_metadata or {})
        metadata.pop("ingest_recovery", None)
        metadata.pop("ingestion_diagnostic", None)
        metadata["manual_ingest_reconciliation"] = {
            "kind": _APPROVAL_KIND,
            "workflow_run_id": plan["temporal"]["run_id"],
            "temporal_status": plan["temporal"]["status"],
            "clip_sha256": clip.sha256,
            "evidence_fingerprint": approved_fingerprint,
            "reconciled_at": datetime.now(UTC).isoformat(),
        }
        source.source_metadata = metadata
        session.add(DomainEvent(
            aggregate_type="source",
            aggregate_id=str(source.id),
            event_type="source.ingest_reconciled_linked_clip",
            payload={
                "source_id": str(source.id),
                "clip_id": str(clip.id),
                "workflow_id": source.workflow_id,
                "workflow_run_id": plan["temporal"]["run_id"],
                "clip_sha256": clip.sha256,
                "evidence_fingerprint": approved_fingerprint,
                "method": _APPROVAL_KIND,
            },
        ))
    return {
        "source_id": str(source_id), "clip_id": str(clip.id),
        "status": SourceStatus.READY.value,
        "reconciled": True,
        "workflow_started": False, "storage_mutated": False,
    }
