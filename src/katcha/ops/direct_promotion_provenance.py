"""Durable byte-verified source claim *before* direct R2 stage-to-raw promotion.

A raw/<sha> object alone does not establish who acquired it. Recording this
checkpoint before moving verified bytes closes the crash window in which a
completed object could otherwise lose all SourceItem provenance.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import SourceStatus
from katcha.integrations.download import canonicalize_url
from katcha.integrations.storage import ObjectStore
from katcha.models import DomainEvent, SourceItem

PROOF_KEY = "direct_promotion_proof"
PROOF_KIND = "verified_direct_stage_v1"
MAX_PROOF_JSON_BYTES = 32768
MAX_MEDIA_BYTES = 4 * 1024**3
_VALID_SHA = re.compile(r"^[0-9a-f]{64}$")
_VALID_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "webm", "mkv"})
_VALID_STAGE = re.compile(
    r"^staging/direct/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})/"
    r"[0-9a-f]{32}\.(mp4|mov|m4v|webm|mkv)$"
)


def proof_fingerprint(proof: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_proof(proof: object, source_id: uuid.UUID) -> bool:
    if not isinstance(proof, dict) or set(proof) != {
        "kind", "source_id", "workflow_id", "canonical_url_sha256",
        "stage_key", "raw_key", "sha256", "size_bytes",
        "extension", "media_metadata",
    }:
        return False
    if proof["kind"] != PROOF_KIND or proof["source_id"] != str(source_id):
        return False
    if proof["workflow_id"] is not None and (
        not isinstance(proof["workflow_id"], str) or len(proof["workflow_id"]) > 255
    ):
        return False
    sha, ext = proof["sha256"], proof["extension"]
    if not isinstance(sha, str) or not _VALID_SHA.fullmatch(sha):
        return False
    if not isinstance(ext, str) or ext not in _VALID_EXTENSIONS:
        return False
    if proof["raw_key"] != ObjectStore.raw_key(sha, ext):
        return False
    if type(proof["size_bytes"]) is not int or not (
        0 < proof["size_bytes"] <= MAX_MEDIA_BYTES
    ):
        return False
    stage = proof["stage_key"]
    if not isinstance(stage, str):
        return False
    match = _VALID_STAGE.fullmatch(stage)
    if match is None or match.group(1) != str(source_id) or match.group(2) != ext:
        return False
    canonical_digest = proof["canonical_url_sha256"]
    if not isinstance(canonical_digest, str) or not _VALID_SHA.fullmatch(canonical_digest):
        return False
    metadata = proof["media_metadata"]
    if not isinstance(metadata, dict):
        return False
    streams = metadata.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in streams
    ):
        return False
    try:
        return len(json.dumps(proof, sort_keys=True).encode()) <= MAX_PROOF_JSON_BYTES
    except (TypeError, ValueError):
        return False


def record_verified_direct_promotion(
    source_id: uuid.UUID,
    *,
    canonical_url: str,
    stage_key: str,
    sha256: str,
    size_bytes: int,
    extension: str,
    media_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Commit verified stage provenance before any raw object is copied.

    A different recorded attempt cannot overwrite an unresolved claim. No
    source URL, signed query string or provider credential enters audit data.
    """
    with session_scope() as session:
        source = session.scalar(
            select(SourceItem).where(SourceItem.id == source_id).with_for_update()
        )
        if source is None or source.status != SourceStatus.INGESTING.value:
            raise RuntimeError("Direct promotion requires an INGESTING source")
        if source.clip_id is not None:
            raise RuntimeError("Direct promotion source already links a Clip")
        if (source.source_metadata or {}).get("remote_acquisition_job_id"):
            raise RuntimeError("Remote acquisition is not a direct stage claim")
        if canonicalize_url(source.source_url) != canonical_url:
            raise RuntimeError("Direct promotion source URL identity changed")
        proof = {
            "kind": PROOF_KIND,
            "source_id": str(source_id),
            "workflow_id": source.workflow_id,
            "canonical_url_sha256": hashlib.sha256(canonical_url.encode()).hexdigest(),
            "stage_key": stage_key,
            "raw_key": ObjectStore.raw_key(sha256, extension),
            "sha256": sha256,
            "size_bytes": size_bytes,
            "extension": extension,
            "media_metadata": media_metadata,
        }
        if not validate_proof(proof, source_id):
            raise ValueError("Invalid verified direct-stage evidence")
        existing = (source.source_metadata or {}).get(PROOF_KEY)
        if existing is not None:
            if existing != proof:
                raise RuntimeError(
                    "An earlier verified direct-media attempt requires reconciliation"
                )
            return proof
        source.source_metadata = {
            **dict(source.source_metadata or {}),
            PROOF_KEY: proof,
        }
        session.add(DomainEvent(
            aggregate_type="source",
            aggregate_id=str(source_id),
            event_type="source.direct_media_stage_verified",
            payload={
                "source_id": str(source_id),
                "sha256": sha256,
                "raw_key": proof["raw_key"],
                "size_bytes": size_bytes,
                "claim_sha256": proof_fingerprint(proof),
            },
        ))
        return proof


def verified_claim_event_present(
    session: Any, source_id: uuid.UUID, proof: dict[str, Any]
) -> bool:
    """Only trust a matching durable internal event, not metadata alone."""
    events = session.scalars(
        select(DomainEvent).where(
            DomainEvent.aggregate_type == "source",
            DomainEvent.aggregate_id == str(source_id),
            DomainEvent.event_type == "source.direct_media_stage_verified",
        )
    )
    expected = proof_fingerprint(proof)
    return any(
        isinstance(event.payload, dict)
        and event.payload.get("claim_sha256") == expected
        and event.payload.get("sha256") == proof["sha256"]
        and event.payload.get("raw_key") == proof["raw_key"]
        for event in events
    )
