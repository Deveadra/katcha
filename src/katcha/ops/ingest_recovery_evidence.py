"""Read-only, bounded evidence inspection for a held SourceItem ingestion.

This is an operator diagnostic, never a permission to replay or clean media.
Presence/absence of a stage prefix cannot prove whether a content-addressed
raw object was written before a crash.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select
from temporalio.client import RPCError, RPCStatusCode, WorkflowExecutionStatus

from katcha.db import session_scope
from katcha.domain import SourceStatus
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, SourceItem

DESCRIBE_TIMEOUT_SECONDS = 8.0
MAX_INVENTORY_RECORDS = 2000
MAX_HASH_BYTES = 4 * 1024**3
_STAGE_SUFFIX = "/"
_TERMINAL = {
    WorkflowExecutionStatus.COMPLETED,
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.CANCELED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
}


def _source_snapshot(source_id: uuid.UUID) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.get(SourceItem, source_id)
        if row is None:
            return None
        clip = session.get(Clip, row.clip_id) if row.clip_id else None
        recovery = (row.source_metadata or {}).get("ingest_recovery")
        safe_code = recovery.get("code") if isinstance(recovery, dict) else None
        if safe_code not in {
            "ingest_temporal_not_running",
            "ingest_temporal_execution_missing",
            "ingest_temporal_visibility_unavailable",
        }:
            safe_code = None
        return {
            "source_id": str(row.id),
            "status": row.status,
            "workflow_id": row.workflow_id,
            "clip_id": str(row.clip_id) if row.clip_id else None,
            "clip_record_found": clip is not None,
            "clip": (
                {
                    "id": str(clip.id),
                    "sha256": clip.sha256,
                    "size_bytes": clip.size_bytes,
                    "storage_key": clip.storage_key,
                    "status": clip.status,
                }
                if clip is not None else None
            ),
            "recovery_code": safe_code,
            "remote_acquisition_job_recorded": bool(
                (row.source_metadata or {}).get("remote_acquisition_job_id")
            ),
        }


async def _describe(client: Any, workflow_id: str | None) -> dict[str, Any]:
    if not workflow_id:
        return {"visibility": "not_recorded", "status": None, "terminal": None}
    if client is None:
        return {"visibility": "unavailable", "status": None, "terminal": None}
    try:
        description = await asyncio.wait_for(
            client.get_workflow_handle(workflow_id).describe(),
            timeout=DESCRIBE_TIMEOUT_SECONDS,
        )
    except RPCError as exc:
        return {
            "visibility": "not_found" if exc.status == RPCStatusCode.NOT_FOUND
            else "unavailable",
            "status": None,
            "terminal": None,
        }
    except Exception:
        # Never write Temporal transport errors or source URLs into the report.
        return {"visibility": "unavailable", "status": None, "terminal": None}
    state = description.status
    if not isinstance(state, WorkflowExecutionStatus):
        return {"visibility": "unverified", "status": None, "terminal": None}
    return {
        "visibility": "verified",
        "status": state.name,
        "terminal": state in _TERMINAL,
    }


def _inventory(store: ObjectStore, source_id: uuid.UUID, limit: int) -> dict[str, Any]:
    prefix = f"staging/direct/{source_id}{_STAGE_SUFFIX}"
    result: dict[str, Any] = {
        "visibility": "verified",
        "prefix": prefix,
        "completed_objects": 0,
        "incomplete_uploads": 0,
        "complete": True,
        "sample_keys": [],
    }
    variants: tuple[tuple[str, str, str], ...] = (
        ("list_objects_v2", "Contents", "completed_objects"),
        ("list_multipart_uploads", "Uploads", "incomplete_uploads"),
    )
    try:
        for operation, collection, counter in variants:
            visited: set[tuple[str, ...]] = set()
            cursor: dict[str, str] = {}
            count = 0
            for _ in range(limit + 1):
                if count >= limit:
                    result["complete"] = False
                    break
                params: dict[str, Any] = {
                    "Bucket": store.settings.s3_bucket,
                    "Prefix": prefix,
                    ("MaxKeys" if collection == "Contents" else "MaxUploads"): min(
                        limit - count, 1000
                    ),
                    **cursor,
                }
                response = getattr(store.client, operation)(**params)
                objects = response.get(collection) or []
                if not isinstance(objects, list) or len(objects) > params[
                    "MaxKeys" if collection == "Contents" else "MaxUploads"
                ]:
                    raise ValueError("Unexpected object listing shape")
                for item in objects:
                    key = item.get("Key") if isinstance(item, dict) else None
                    if not isinstance(key, str) or not key.startswith(prefix):
                        raise ValueError("Out-of-scope storage object returned")
                    if collection == "Uploads" and not item.get("UploadId"):
                        raise ValueError("Incomplete upload missing identity")
                    count += 1
                    result[counter] += 1
                    if len(result["sample_keys"]) < 8:
                        result["sample_keys"].append(key)
                if not response.get("IsTruncated"):
                    break
                if collection == "Contents":
                    token = response.get("NextContinuationToken")
                    if not isinstance(token, str) or not token:
                        raise ValueError("Storage listing omitted pagination token")
                    next_cursor = {"ContinuationToken": token}
                else:
                    key_marker = response.get("NextKeyMarker")
                    upload_marker = response.get("NextUploadIdMarker")
                    if not all(isinstance(v, str) and v for v in (key_marker, upload_marker)):
                        raise ValueError("Multipart listing omitted pagination marker")
                    next_cursor = {
                        "KeyMarker": key_marker,
                        "UploadIdMarker": upload_marker,
                    }
                identity = tuple(next_cursor.values())
                if identity in visited:
                    raise ValueError("Storage pagination cursor repeated")
                visited.add(identity)
                cursor = next_cursor
                if count >= limit:
                    result["complete"] = False
                    break
            else:
                raise ValueError("Storage pagination exceeded bounded steps")
    except Exception:
        # Partial counts are evidence of uncertainty, not an empty bucket.
        result["visibility"] = "unavailable"
        result["complete"] = False
    return result


def _clip_storage(
    store: ObjectStore,
    clip: dict[str, Any] | None,
    *,
    verify_sha256: bool,
) -> dict[str, Any]:
    if clip is None:
        return {"state": "no_linked_clip", "sha256_check": "not_applicable"}
    key = clip["storage_key"]
    if not isinstance(key, str) or not key.startswith("raw/"):
        return {"state": "unexpected_storage_key", "sha256_check": "not_checked"}
    try:
        stat = store.stat(key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return {
            "state": "missing" if code in {"404", "NoSuchKey", "NotFound"} else "unavailable",
            "sha256_check": "not_checked",
        }
    except Exception:
        return {"state": "unavailable", "sha256_check": "not_checked"}
    actual_size = stat["size_bytes"]
    expected_size = clip.get("size_bytes")
    if not isinstance(actual_size, int) or actual_size <= 0:
        return {"state": "invalid_size", "sha256_check": "not_checked"}
    if expected_size is not None and actual_size != expected_size:
        return {
            "state": "size_mismatch",
            "stored_size_bytes": actual_size,
            "sha256_check": "not_checked",
        }
    summary: dict[str, Any] = {
        "state": "head_matches_record",
        "stored_size_bytes": actual_size,
        "sha256_check": "not_requested",
    }
    if not verify_sha256:
        return summary
    if actual_size > MAX_HASH_BYTES:
        summary["sha256_check"] = "exceeds_verification_limit"
        return summary
    digest = hashlib.sha256()
    total = 0
    try:
        for chunk in store.iter_bytes(key, chunk_size=1024 * 1024):
            total += len(chunk)
            if total > actual_size:
                summary["sha256_check"] = "mismatch"
                return summary
            digest.update(chunk)
    except Exception:
        summary["sha256_check"] = "unavailable"
        return summary
    summary["sha256_check"] = (
        "verified" if total == actual_size and digest.hexdigest() == clip["sha256"]
        else "mismatch"
    )
    return summary


async def inspect_held_ingest(
    source_id: uuid.UUID,
    *,
    temporal_client: Any = None,
    store: ObjectStore | None = None,
    max_records: int = 200,
    verify_sha256: bool = False,
) -> dict[str, Any]:
    """Never mutates source, Temporal or R2; never authorizes a retry."""
    if max_records < 1 or max_records > MAX_INVENTORY_RECORDS:
        raise ValueError("max_records must be between 1 and 2000")
    before = _source_snapshot(source_id)
    if before is None:
        return {
            "source_id": str(source_id),
            "assessment": "source_not_found",
            "retry_authorized": False,
        }
    temporal = await _describe(temporal_client, before["workflow_id"])
    try:
        store = store or ObjectStore()
        inventory = _inventory(store, source_id, max_records)
        clip_state = _clip_storage(
            store, before["clip"], verify_sha256=verify_sha256
        )
    except Exception:
        inventory = {
            "visibility": "unavailable", "complete": False,
            "completed_objects": 0, "incomplete_uploads": 0,
        }
        clip_state = {"state": "unavailable", "sha256_check": "not_checked"}
    after = _source_snapshot(source_id)
    stale = after is None or any(
        before[field] != after[field]
        for field in ("status", "workflow_id", "clip_id", "clip")
    )
    if stale:
        assessment = "source_changed_during_inspection"
    elif before["status"] != SourceStatus.INGESTING.value:
        assessment = "not_an_ingesting_source"
    elif before["remote_acquisition_job_recorded"]:
        assessment = "remote_acquisition_requires_separate_reconciliation"
    elif temporal["visibility"] != "verified" or not inventory["complete"]:
        assessment = "evidence_incomplete"
    elif temporal["status"] == WorkflowExecutionStatus.RUNNING.name:
        assessment = "temporal_running_no_replay"
    elif before["clip_id"] or inventory["completed_objects"] or inventory[
        "incomplete_uploads"
    ]:
        assessment = "media_or_staging_requires_review"
    else:
        # Even when the exact stage prefix is empty, an unlinked raw/ object
        # might have been copied before persistence of Clip/SourceItem.
        assessment = "terminal_requires_unlinked_raw_review"
    return {
        "source_id": str(source_id),
        "source_status": before["status"],
        "workflow_id": before["workflow_id"],
        "recovery_code": before["recovery_code"],
        "linked_clip_id": before["clip_id"],
        "clip_record_found": before["clip_record_found"],
        "temporal": temporal,
        "direct_staging": inventory,
        "linked_clip_storage": clip_state,
        "source_changed_during_inspection": stale,
        "assessment": assessment,
        "unlinked_raw_object_not_excluded": True,
        "retry_authorized": False,
        "operator_next_step": (
            "Reconcile Temporal history and any raw/ content-addressed objects "
            "before reviewing a distinct retry identity. This report is read-only."
        ),
        "observed_at": datetime.now(UTC).isoformat(),
    }
