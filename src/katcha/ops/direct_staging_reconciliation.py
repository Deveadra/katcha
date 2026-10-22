"""Conservative, explicitly invoked reconciliation of direct-media R2 staging.

Only keys made by _ingest_direct_source are eligible. Remote acquisition job
staging (acquisition-staging/) is intentionally excluded. No automatic timer,
workflow restart, unknown-source deletion or entitlement changes are implied.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import SourceStatus
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, SourceItem
from katcha.ops.direct_promotion_provenance import PROOF_KEY
from katcha.runtime_fence import assert_mutation_authority

DIRECT_STAGE_PREFIX = "staging/direct/"
MINIMUM_AGE_HOURS = 24
MAX_SCAN_RECORDS = 2000
_STAGING_KEY = re.compile(
    r"^staging/direct/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})/"
    r"([0-9a-f]{32})\.(mp4|mov|m4v|webm|mkv)$"
)
_TERMINAL_SOURCE_STATUSES = {SourceStatus.READY.value, SourceStatus.FAILED.value}


def stage_source_id(key: str) -> uuid.UUID | None:
    match = _STAGING_KEY.fullmatch(key)
    if match is None:
        return None
    value = uuid.UUID(match.group(1))
    if str(value) != match.group(1):
        return None
    return value


def older_than_cutoff(timestamp: object, cutoff: datetime) -> bool:
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        return False
    return timestamp.astimezone(UTC) < cutoff


def _iter_objects(store: ObjectStore, limit: int):
    token: str | None = None
    seen: set[str] = set()
    count = 0
    pages = 0
    while count < limit:
        pages += 1
        if pages > limit:
            raise RuntimeError("Staging object listing exceeded its page bound")
        args: dict[str, Any] = {
            "Bucket": store.settings.s3_bucket,
            "Prefix": DIRECT_STAGE_PREFIX,
            "MaxKeys": min(1000, limit - count),
        }
        if token is not None:
            args["ContinuationToken"] = token
        response = store.client.list_objects_v2(**args)
        for obj in response.get("Contents") or []:
            if count >= limit:
                return
            count += 1
            yield obj
        if not response.get("IsTruncated"):
            return
        following = response.get("NextContinuationToken")
        if not isinstance(following, str) or not following or following in seen:
            raise RuntimeError("Staging object listing returned an invalid pagination cursor")
        seen.add(following)
        token = following


def _iter_uploads(store: ObjectStore, limit: int):
    key_marker: str | None = None
    upload_marker: str | None = None
    seen: set[tuple[str, str]] = set()
    count = 0
    pages = 0
    while count < limit:
        pages += 1
        if pages > limit:
            raise RuntimeError("Staging upload listing exceeded its page bound")
        args: dict[str, Any] = {
            "Bucket": store.settings.s3_bucket,
            "Prefix": DIRECT_STAGE_PREFIX,
            "MaxUploads": min(1000, limit - count),
        }
        if key_marker is not None:
            args["KeyMarker"] = key_marker
            if upload_marker is not None:
                args["UploadIdMarker"] = upload_marker
        response = store.client.list_multipart_uploads(**args)
        for upload in response.get("Uploads") or []:
            if count >= limit:
                return
            count += 1
            yield upload
        if not response.get("IsTruncated"):
            return
        following_key = response.get("NextKeyMarker")
        following_upload = response.get("NextUploadIdMarker")
        cursor = (following_key, following_upload)
        if (
            not isinstance(following_key, str) or not following_key
            or not isinstance(following_upload, str) or not following_upload
            or cursor in seen
        ):
            raise RuntimeError("Staging upload listing returned an invalid pagination cursor")
        seen.add(cursor)
        key_marker, upload_marker = cursor


def _has_unresolved_promotion_proof(source: SourceItem, key: str) -> bool:
    """A stage may be the last valid copy after a failed raw/ promotion.

    A prior failed source can also have an earlier incomplete upload with a
    different attempt ID. Hold *all* that source's stages until the incident
    is reconciled rather than trusting the terminal status alone.
    """
    proof = (source.source_metadata or {}).get(PROOF_KEY)
    if proof is None:
        return False
    if source.status != SourceStatus.READY.value or source.clip_id is None:
        return True
    return isinstance(proof, dict) and proof.get("stage_key") == key


def _eligible_source(key: str, *, lock: bool) -> tuple[bool, str]:
    source_id = stage_source_id(key)
    if source_id is None:
        return False, "unrecognized_key"
    with session_scope() as session:
        query = select(SourceItem).where(SourceItem.id == source_id)
        if lock:
            query = query.with_for_update()
        source = session.scalar(query)
        if source is None:
            return False, "unknown_source"
        if source.status not in _TERMINAL_SOURCE_STATUSES:
            return False, "source_not_terminal"
        if _has_unresolved_promotion_proof(source, key):
            return False, "provenance_claim_pending"
        if session.scalar(select(Clip.id).where(Clip.storage_key == key).limit(1)):
            return False, "referenced_by_clip"
    return True, "eligible"


def _remove_if_eligible(store: ObjectStore, key: str, *, kind: str, upload_id: str | None):
    """Recheck while locking the SourceItem; never trust the earlier dry-run view."""
    source_id = stage_source_id(key)
    if source_id is None:
        return "unrecognized_key"
    with session_scope() as session:
        source = session.scalar(
            select(SourceItem).where(SourceItem.id == source_id).with_for_update()
        )
        if source is None:
            return "unknown_source"
        if source.status not in _TERMINAL_SOURCE_STATUSES:
            return "source_not_terminal"
        if _has_unresolved_promotion_proof(source, key):
            return "provenance_claim_pending"
        if session.scalar(select(Clip.id).where(Clip.storage_key == key).limit(1)):
            return "referenced_by_clip"
        if kind == "object":
            store.delete(key)
        else:
            if not upload_id:
                return "missing_upload_id"
            assert_mutation_authority(
                "storage.multipart_upload_abort", settings=store.settings
            )
            store.client.abort_multipart_upload(
                Bucket=store.settings.s3_bucket, Key=key, UploadId=upload_id
            )
    return "removed"


def reconcile_direct_staging(
    *,
    apply: bool = False,
    min_age_hours: int = 24,
    max_records: int = 200,
    store: ObjectStore | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Inspect aged stage leftovers; --apply deletes only terminal-source leftovers.

    Nothing is changed by default. A SourceItem in INGESTING or unknown state is
    never removed, even if its staging object is extremely old. Incomplete
    uploads and completed stage objects use the same conservative rules.
    """
    if min_age_hours < MINIMUM_AGE_HOURS:
        raise ValueError("Staging retention must be at least 24 hours")
    if max_records < 1 or max_records > MAX_SCAN_RECORDS:
        raise ValueError("Staging scan record limit must be between 1 and 2000")
    anchor = now or datetime.now(UTC)
    if anchor.tzinfo is None:
        raise ValueError("Staging reconciliation requires timezone-aware time")
    cutoff = anchor.astimezone(UTC) - timedelta(hours=min_age_hours)
    store = store or ObjectStore()
    counts: Counter[str] = Counter()
    previews: list[dict[str, str]] = []

    # Inventory both sources before touching S3. In apply mode, a failed object
    # or multipart listing must not leave half the previous page already deleted.
    inventories = (
        ("object", list(_iter_objects(store, max_records)), "LastModified"),
        ("multipart", list(_iter_uploads(store, max_records)), "Initiated"),
    )
    for kind, rows, timestamp_field in inventories:
        for item in rows:
            counts["scanned"] += 1
            key = item.get("Key")
            if not isinstance(key, str) or not key.startswith(DIRECT_STAGE_PREFIX):
                counts["out_of_scope"] += 1
                continue
            if not older_than_cutoff(item.get(timestamp_field), cutoff):
                counts["recent_or_undated"] += 1
                continue
            upload_id = item.get("UploadId") if kind == "multipart" else None
            if kind == "multipart" and (
                not isinstance(upload_id, str) or not upload_id
            ):
                counts["missing_upload_id"] += 1
                continue
            allowed, reason = _eligible_source(key, lock=False)
            if not allowed:
                counts[reason] += 1
                continue
            counts["eligible"] += 1
            if len(previews) < 20:
                # Only structured UUID staging keys, never source URLs or upload IDs.
                previews.append({"kind": kind, "key": key})
            if apply:
                result = _remove_if_eligible(
                    store, key, kind=kind, upload_id=upload_id
                )
                counts["removed" if result == "removed" else result] += 1

    return {
        "mode": "apply" if apply else "dry_run",
        "scope": DIRECT_STAGE_PREFIX,
        "min_age_hours": min_age_hours,
        "max_records_per_inventory": max_records,
        "counts": dict(counts),
        "candidate_examples": previews,
        "remote_acquisition_staging_excluded": True,
    }
