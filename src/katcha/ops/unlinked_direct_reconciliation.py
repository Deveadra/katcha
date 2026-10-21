"""Explicit, provenance-bound restoration of an unlinked direct R2 raw object.

Only a claim written after stage byte/video validation and BEFORE raw promotion
can establish SourceItem ownership. Never infer provenance from raw key or SHA
alone. No download, Temporal replay, R2 mutation, or automatic scheduling.
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
from katcha.integrations.download import canonicalize_url
from katcha.integrations.object_streaming import inspect_stored_media, verify_stored_sha256
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.ops.direct_promotion_provenance import (
    PROOF_KEY,
    proof_fingerprint,
    validate_proof,
    verified_claim_event_present,
)
from katcha.ops.ingest_recovery_evidence import inspect_held_ingest
from katcha.ops.linked_ingest_reconciliation import RecoveryApprovalError
from katcha.orchestration.activities import _extract_video_shape

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"})
_METHOD = "unlinked_verified_direct_provenance_v1"


def _source_state(source_id: uuid.UUID) -> dict[str, Any] | None:
    with session_scope() as session:
        source = session.get(SourceItem, source_id)
        if source is None:
            return None
        raw = dict(source.source_metadata or {})
        claim = raw.get(PROOF_KEY)
        claim = claim if validate_proof(claim, source_id) else None
        has_event = (
            verified_claim_event_present(session, source_id, claim)
            if claim is not None else False
        )
        canonical_digest = hashlib.sha256(
            canonicalize_url(source.source_url).encode()
        ).hexdigest()
        collision = (
            session.scalar(select(Clip.id).where(Clip.sha256 == claim["sha256"]))
            if claim is not None else None
        )
        return {
            "source_id": str(source.id),
            "status": source.status,
            "clip_id": str(source.clip_id) if source.clip_id else None,
            "workflow_id": source.workflow_id,
            "source_digest": hashlib.sha256(
                json.dumps(raw, sort_keys=True).encode()
            ).hexdigest(),
            "canonical_digest": canonical_digest,
            "remote_acquisition": bool(raw.get("remote_acquisition_job_id")),
            "claim": claim,
            "claim_event_matches": has_event,
            "clip_sha_collision": collision is not None,
            "source_updated_at": (
                source.updated_at.isoformat() if source.updated_at else None
            ),
        }


def _raw_evidence(store: ObjectStore, claim: dict[str, Any]) -> dict[str, str]:
    key, size, sha = claim["raw_key"], claim["size_bytes"], claim["sha256"]
    try:
        stat = store.stat(key)
    except Exception:
        return {"state": "unavailable", "hash": "unverified", "video": "unverified"}
    if stat.get("size_bytes") != size:
        return {"state": "length_mismatch", "hash": "unverified", "video": "unverified"}
    try:
        verify_stored_sha256(store, key, sha, size)
    except Exception:
        return {"state": "head_matches", "hash": "unverified", "video": "unverified"}
    try:
        inspected = inspect_stored_media(store, key, size)
    except Exception:
        return {"state": "head_matches", "hash": "verified", "video": "unverified"}
    streams = inspected.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in streams
    ):
        return {"state": "head_matches", "hash": "verified", "video": "invalid"}
    # Re-probe a fresh object for decodable video. The claim already stores
    # source-stage inspection metadata for the later Clip record.
    return {"state": "head_matches", "hash": "verified", "video": "verified"}


async def plan_unlinked_direct_recovery(
    source_id: uuid.UUID,
    *,
    temporal_client: Any = None,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    """Conservative full-byte evidence preview, no mutations."""
    snapshot = _source_state(source_id)
    if snapshot is None:
        return {
            "source_id": str(source_id),
            "eligible": False,
            "blockers": ["source_missing"],
            "approval_fingerprint": None,
            "retry_authorized": False,
        }
    report = await inspect_held_ingest(
        source_id, temporal_client=temporal_client, store=store,
        max_records=2000, verify_sha256=False,
    )
    current = _source_state(source_id)
    reasons: list[str] = []
    if snapshot != current or report.get("source_changed_during_inspection"):
        reasons.append("source_changed")
    if snapshot["status"] != SourceStatus.INGESTING.value:
        reasons.append("source_not_ingesting")
    if snapshot["clip_id"]:
        reasons.append("already_linked")
    if snapshot["remote_acquisition"]:
        reasons.append("leased_remote_acquisition_excluded")
    claim = snapshot["claim"]
    if claim is None:
        reasons.append("durable_provenance_missing")
    else:
        if not snapshot["claim_event_matches"]:
            reasons.append("verified_promotion_event_missing")
        if snapshot["clip_sha_collision"]:
            reasons.append("existing_clip_collision_requires_review")
        if claim["workflow_id"] != snapshot["workflow_id"] or not snapshot["workflow_id"]:
            reasons.append("workflow_identity_changed")
        if claim["canonical_url_sha256"] != snapshot["canonical_digest"]:
            reasons.append("canonical_source_changed")

    temporal = report.get("temporal") or {}
    if (
        temporal.get("visibility") != "verified"
        or temporal.get("status") not in _TERMINAL
        or not temporal.get("run_id")
    ):
        reasons.append("terminal_temporal_run_unverified")
    stage = report.get("direct_staging") or {}
    if stage.get("visibility") != "verified" or not stage.get("complete"):
        reasons.append("stage_inventory_incomplete")
    if stage.get("completed_objects") or stage.get("incomplete_uploads"):
        reasons.append("staging_not_reconciled")

    raw_evidence = {"state": "not_checked", "hash": "unverified", "video": "unverified"}
    if claim is not None and not reasons:
        try:
            raw_evidence = _raw_evidence(store or ObjectStore(), claim)
        except Exception:
            raw_evidence = {"state": "unavailable", "hash": "unverified", "video": "unverified"}
        if raw_evidence != {
            "state": "head_matches", "hash": "verified", "video": "verified"
        }:
            reasons.append("raw_object_not_verified")

    after = _source_state(source_id)
    if snapshot != after:
        reasons.append("source_changed")
    eligible = not reasons
    basis = {
        "kind": _METHOD,
        "source": snapshot,
        "temporal": temporal,
        "stage": {
            "complete": stage.get("complete"),
            "objects": stage.get("completed_objects"),
            "uploads": stage.get("incomplete_uploads"),
        },
        "raw_evidence": raw_evidence,
    }
    approval = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() if eligible else None
    return {
        "source_id": str(source_id),
        "eligible": eligible,
        "blockers": sorted(set(reasons)),
        "approval_fingerprint": approval,
        "proof_sha256": claim["sha256"] if claim is not None else None,
        "provenance_event_verified": snapshot["claim_event_matches"],
        "temporal": temporal,
        "staging": stage,
        "raw_evidence": raw_evidence,
        "source_snapshot": snapshot if eligible else None,
        "retry_authorized": False,
        "proposed_action": "create_clip_from_verified_claim" if eligible else "no_action",
    }


async def finalize_unlinked_direct_recovery(
    source_id: uuid.UUID,
    *,
    approved_fingerprint: str,
    temporal_client: Any = None,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    """Requires a reviewed exact approval, then a guarded one-transaction link."""
    if not isinstance(approved_fingerprint, str) or not _HEX64.fullmatch(
        approved_fingerprint
    ):
        raise RecoveryApprovalError("A full 64-character approval fingerprint is required")
    proposal = await plan_unlinked_direct_recovery(
        source_id, temporal_client=temporal_client, store=store,
    )
    if not proposal["eligible"] or proposal["approval_fingerprint"] != approved_fingerprint:
        raise RecoveryApprovalError("Direct media evidence is stale, incomplete or unapproved")
    expected = proposal["source_snapshot"]
    assert expected is not None
    claim = expected["claim"]
    assert claim is not None

    with session_scope() as session:
        source = session.scalar(
            select(SourceItem).where(SourceItem.id == source_id).with_for_update()
        )
        if source is None:
            raise RecoveryApprovalError("Source vanished during recovery")
        metadata = dict(source.source_metadata or {})
        # The external evidence is deliberately checked before acquiring the
        # DB lock; this recheck fences any concurrent SourceItem mutation.
        if (
            source.status != expected["status"]
            or (str(source.clip_id) if source.clip_id else None) != expected["clip_id"]
            or source.workflow_id != expected["workflow_id"]
            or hashlib.sha256(canonicalize_url(source.source_url).encode()).hexdigest()
            != expected["canonical_digest"]
            or metadata.get(PROOF_KEY) != claim
            or hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
            != expected["source_digest"]
            or bool(metadata.get("remote_acquisition_job_id"))
            or not verified_claim_event_present(session, source_id, claim)
            or session.scalar(select(Clip.id).where(Clip.sha256 == claim["sha256"]))
            is not None
        ):
            raise RecoveryApprovalError("Source/Clip provenance changed before commit")
        duration, width, height = _extract_video_shape(claim["media_metadata"])
        clip = Clip(
            duration_seconds=duration, width=width, height=height,
            sha256=claim["sha256"],
            storage_key=claim["raw_key"],
            extension=claim["extension"],
            size_bytes=claim["size_bytes"],
            status=ClipStatus.INGESTED.value,
            media_metadata=claim["media_metadata"],
        )
        session.add(clip)
        session.flush()
        source.clip_id = clip.id
        source.status = SourceStatus.READY.value
        source.error = None
        metadata.pop("ingest_recovery", None)
        metadata.pop("ingestion_diagnostic", None)
        metadata["manual_ingest_reconciliation"] = {
            "kind": _METHOD,
            "claim_sha256": proof_fingerprint(claim),
            "workflow_run_id": proposal["temporal"]["run_id"],
            "evidence_fingerprint": approved_fingerprint,
            "reconciled_at": datetime.now(UTC).isoformat(),
        }
        source.source_metadata = metadata
        session.add(DomainEvent(
            aggregate_type="source",
            aggregate_id=str(source.id),
            event_type="source.ingest_reconciled_verified_unlinked_raw",
            payload={
                "source_id": str(source.id), "clip_id": str(clip.id),
                "sha256": clip.sha256,
                "claim_sha256": proof_fingerprint(claim),
                "workflow_run_id": proposal["temporal"]["run_id"],
                "evidence_fingerprint": approved_fingerprint,
            },
        ))
        clip_id = str(clip.id)
    return {
        "source_id": str(source_id),
        "clip_id": clip_id,
        "status": SourceStatus.READY.value,
        "reconciled": True,
        "workflow_started": False,
        "storage_mutated": False,
    }
