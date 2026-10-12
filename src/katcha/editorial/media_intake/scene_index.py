"""Rights-gated durable visual indexes over existing canonical Clip bytes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
from katcha.db import session_scope
from katcha.editorial.media_intake.scene_schemas import SceneIndexV1, SearchShotsRequest
from katcha.editorial.media_intake.scene_worker import MAX_RESULT_BYTES
from katcha.editorial_models import AcquisitionReceipt, MediaAssetValidation
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, SourceItem
from katcha.scene_index_models import MediaSceneIndex
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_media_bin import get_project_media_item
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound

MAX_INDEX_BYTES = 200 * 1024 * 1024
MAX_SEARCH_BYTES = 512 * 1024 * 1024
POLICY_VERSION = "bounded-local-video-v1"
WALL_SECONDS = 90
_DECODER_SLOT = threading.BoundedSemaphore(1)


@dataclass(frozen=True)
class _Source:
    clip_id: uuid.UUID
    sha256: str
    key: str
    permission_digest: str
    declared_size: int


def _source(channel_id: uuid.UUID, project_id: uuid.UUID, item_id: uuid.UUID) -> _Source:
    item = get_project_media_item(channel_id, project_id, item_id)
    if item.clip_id is None or item.kind != "video" or item.status not in {"acquired", "ready"}:
        raise EditorialConflict("Indexing requires an acquired canonical video")
    if item.rights_status in {"restricted", "denied", "revoked", "expired", "reference_only"}:
        raise EditorialConflict("Current permission does not allow media inspection")
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        clip = session.get(Clip, item.clip_id)
        if (
            clip is None
            or not clip.storage_key
            or not 0 < (clip.size_bytes or 0) <= MAX_INDEX_BYTES
        ):
            raise EditorialConflict("Canonical media exceeds the indexing size budget or is absent")
        sources = list(session.scalars(select(SourceItem).where(SourceItem.clip_id == clip.id)))
        owned = [
            source
            for source in sources
            if source.platform == "upload"
            and (source.source_metadata or {}).get("permitted_use_confirmed") is True
            and str((source.source_metadata or {}).get("channel_profile_id")) == str(channel_id)
        ]
        permission: dict[str, Any] = {"status": item.status, "rights": item.rights_status}
        if item.rights_assessment_id is not None:
            assessment = session.get(RightsAssessment, item.rights_assessment_id)
            candidate = (
                session.get(DiscoveryCandidate, assessment.discovery_candidate_id)
                if assessment
                else None
            )
            source = (
                session.get(SourceItem, candidate.source_item_id)
                if candidate and candidate.source_item_id
                else None
            )
            latest = (
                session.scalar(
                    select(RightsAssessment)
                    .where(RightsAssessment.discovery_candidate_id == candidate.id)
                    .order_by(RightsAssessment.version.desc())
                )
                if candidate
                else None
            )
            if (
                assessment is None
                or candidate is None
                or source is None
                or latest is None
                or latest.id != assessment.id
                or not latest.production_eligible
                or assessment.version != item.rights_assessment_version
                or str((candidate.candidate_metadata or {}).get("channel_profile_id"))
                != str(channel_id)
                or source.clip_id != clip.id
            ):
                raise EditorialConflict("Current matching rights evidence is required")
            receipt = session.scalar(
                select(AcquisitionReceipt)
                .where(
                    AcquisitionReceipt.channel_profile_id == channel_id,
                    AcquisitionReceipt.candidate_id == candidate.id,
                    AcquisitionReceipt.status == "succeeded",
                    AcquisitionReceipt.sha256 == clip.sha256,
                )
                .order_by(AcquisitionReceipt.created_at.desc(), AcquisitionReceipt.id.desc())
            )
            validation = (
                session.scalar(
                    select(MediaAssetValidation)
                    .where(
                        MediaAssetValidation.receipt_id == receipt.id,
                        MediaAssetValidation.channel_profile_id == channel_id,
                    )
                    .order_by(MediaAssetValidation.probe_version.desc())
                )
                if receipt
                else None
            )
            if (
                validation is None
                or validation.status != "validated"
                or validation.media_type != "video"
                or validation.actual_sha256 != clip.sha256
                or validation.expected_sha256 != clip.sha256
            ):
                raise EditorialConflict("Latest successful video integrity evidence is required")
            permission.update(assessment=str(assessment.id), validation=str(validation.id))
        elif owned:
            permission["upload_evidence"] = sorted(str(source.id) for source in owned)
        else:
            raise EditorialConflict(
                "Recorded operator upload permission or current rights evidence is required"
            )
        return _Source(
            clip.id,
            clip.sha256,
            clip.storage_key,
            hashlib.sha256(json.dumps(permission, sort_keys=True).encode()).hexdigest(),
            int(clip.size_bytes or 0),
        )


def _identity(channel_id: uuid.UUID, source: _Source) -> uuid.UUID:
    return uuid.uuid5(channel_id, f"scene:{source.clip_id}:{source.sha256}:{POLICY_VERSION}")


def _stage(
    source: _Source, store: ObjectStore, path: Path, *, maximum: int = MAX_INDEX_BYTES
) -> int:
    digest = hashlib.sha256()
    size = 0
    with path.open("wb") as output:
        for chunk in store.iter_bytes(source.key, chunk_size=1024 * 1024):
            size += len(chunk)
            if size > min(maximum, MAX_INDEX_BYTES):
                raise EditorialConflict("Media exceeds the indexing/search byte budget")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != source.sha256 or size != source.declared_size:
        raise EditorialConflict("Media bytes changed; refresh canonical integrity evidence")
    return size


def _measure(path: Path, sha256: str, directory: Path) -> SceneIndexV1:
    result = directory / "result.json"
    with (directory / "decoder.log").open("wb") as errors:
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "katcha.editorial.media_intake.scene_worker",
                    str(path),
                    str(result),
                    sha256,
                ],
                check=True,
                timeout=WALL_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=errors,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EditorialConflict(
                "Video inspection failed or exceeded its bounded worker budget"
            ) from exc
    if not result.exists() or result.stat().st_size > MAX_RESULT_BYTES:
        raise EditorialConflict("Measured index evidence is unavailable or oversized")
    evidence = SceneIndexV1.model_validate_json(result.read_bytes())
    if evidence.clip_sha256 != sha256:
        raise EditorialConflict("Measured evidence does not match canonical media")
    return evidence


def index_project_media(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    actor: str,
    retry_failed: bool = False,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    source = _source(channel_id, project_id, item_id)
    index_id = _identity(channel_id, source)
    replay = False
    token = str(uuid.uuid4())
    with session_scope() as session:
        row = session.get(MediaSceneIndex, index_id)
        if row is None:
            row = MediaSceneIndex(
                id=index_id,
                channel_profile_id=channel_id,
                clip_id=source.clip_id,
                clip_sha256=source.sha256,
                policy_version=POLICY_VERSION,
                status="indexing",
                evidence={},
                receipt={
                    "actor": actor,
                    "lease_token": token,
                    "started_at": datetime.now(UTC).isoformat(),
                    "attempt": 1,
                    "vision_provider_calls": 0,
                },
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError as exc:
                raise EditorialConflict("Indexing already started; refresh saved evidence") from exc
        elif row.status == "completed":
            replay = True
        elif row.status == "failed" and retry_failed:
            claimed = session.scalar(
                update(MediaSceneIndex)
                .where(
                    MediaSceneIndex.id == index_id,
                    MediaSceneIndex.status == "failed",
                )
                .values(
                    status="indexing",
                    error_category=None,
                    receipt={
                        "actor": actor,
                        "lease_token": token,
                        "started_at": datetime.now(UTC).isoformat(),
                        "attempt": int(row.receipt.get("attempt", 1)) + 1,
                        "vision_provider_calls": 0,
                    },
                )
                .returning(MediaSceneIndex.id)
            )
            if claimed is None:
                raise EditorialConflict("Indexing already started; refresh saved evidence")
        else:
            raise EditorialConflict(
                "Indexing is unfinished or failed; inspect its receipt before retrying"
            )
    try:
        with tempfile.TemporaryDirectory(prefix="katcha-scene-index-") as temp:
            directory = Path(temp)
            path = directory / "source.media"
            size = _stage(source, store or ObjectStore(), path)
            if _source(channel_id, project_id, item_id) != source:
                raise EditorialConflict(
                    "Media permission or canonical source changed during inspection"
                )
            if replay:
                return get_project_scene_index(channel_id, project_id, item_id, verify_bytes=False)
            if not _DECODER_SLOT.acquire(blocking=False):
                raise EditorialConflict(
                    "Another video inspection is running; retry after it finishes"
                )
            try:
                evidence = _measure(path, source.sha256, directory)
            finally:
                _DECODER_SLOT.release()
            if _source(channel_id, project_id, item_id) != source:
                raise EditorialConflict(
                    "Media permission or canonical source changed during inspection"
                )
        with session_scope() as session:
            row = session.get(MediaSceneIndex, index_id)
            assert row is not None
            receipt = {
                **row.receipt,
                "completed_at": datetime.now(UTC).isoformat(),
                "bytes_read": size,
                "decoded_frames": evidence.decoded_frames,
                "cpu_seconds_limit": 60,
                "wall_seconds_limit": WALL_SECONDS,
                "memory_bytes_limit": 1536 * 1024 * 1024,
                "evidence_bytes_limit": MAX_RESULT_BYTES,
            }
            saved = session.scalar(
                update(MediaSceneIndex)
                .where(
                    MediaSceneIndex.id == index_id,
                    MediaSceneIndex.status == "indexing",
                    MediaSceneIndex.receipt["lease_token"].as_string() == token,
                )
                .values(
                    evidence=evidence.model_dump(mode="json"), status="completed", receipt=receipt
                )
                .returning(MediaSceneIndex.id)
            )
            if saved is None:
                raise EditorialConflict("Indexing attempt was replaced; reload saved evidence")
    except BaseException:
        if not replay:
            with session_scope() as session:
                session.execute(
                    update(MediaSceneIndex)
                    .where(
                        MediaSceneIndex.id == index_id,
                        MediaSceneIndex.status == "indexing",
                        MediaSceneIndex.receipt["lease_token"].as_string() == token,
                    )
                    .values(status="failed", error_category="inspection_failed_or_stale")
                )
        raise
    return get_project_scene_index(channel_id, project_id, item_id, verify_bytes=False)


def get_project_scene_index(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    store: ObjectStore | None = None,
    verify_bytes: bool = True,
) -> dict[str, Any]:
    source = _source(channel_id, project_id, item_id)
    with session_scope() as session:
        row = session.get(MediaSceneIndex, _identity(channel_id, source))
        if row is None:
            raise EditorialNotFound("No measured index for the current media bytes and policy")
        result = {
            "index_id": str(row.id),
            "status": row.status,
            "error_category": row.error_category,
            "receipt": row.receipt,
            "evidence": row.evidence,
        }
    if result["status"] == "completed":
        evidence = SceneIndexV1.model_validate(result["evidence"])
        if evidence.clip_sha256 != source.sha256:
            raise EditorialConflict("Stored index does not match canonical media bytes")
        if verify_bytes:
            with tempfile.TemporaryDirectory(prefix="katcha-scene-check-") as temp:
                _stage(source, store or ObjectStore(), Path(temp) / "source.media")
        if _source(channel_id, project_id, item_id) != source:
            raise EditorialConflict("Current permission or media changed; reload index evidence")
    return result


def search_project_shots(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    request: SearchShotsRequest,
    *,
    store: ObjectStore | None = None,
) -> list[dict[str, Any]]:
    # A bounded exact visual-token search. No semantic/scene-action claims.
    sources = {item_id: _source(channel_id, project_id, item_id) for item_id in request.item_ids}
    terms = set(request.query.casefold().split())
    if not terms <= {"red", "green", "blue", "dark", "bright", "mid-tone"}:
        return []
    if (
        sum(
            source.declared_size
            for source in {source.clip_id: source for source in sources.values()}.values()
        )
        > MAX_SEARCH_BYTES
    ):
        raise EditorialConflict("Search selection exceeds the media verification byte budget")
    results = []
    verified: set[uuid.UUID] = set()
    for item_id, source in sources.items():
        index = get_project_scene_index(
            channel_id,
            project_id,
            item_id,
            store=store,
            verify_bytes=source.clip_id not in verified,
        )
        verified.add(source.clip_id)
        if index["status"] != "completed":
            continue
        evidence = SceneIndexV1.model_validate(index["evidence"])
        scale = evidence.timebase_numerator / evidence.timebase_denominator
        for shot in evidence.shots:
            if terms <= set(shot.visual_tags):
                results.append(
                    {
                        "item_id": str(item_id),
                        "clip_sha256": source.sha256,
                        "index_id": index["index_id"],
                        "shot_id": shot.shot_id,
                        "source_in_seconds": shot.start_tick * scale,
                        "source_out_seconds": shot.end_tick * scale,
                        "keyframe_seconds": shot.keyframe_tick * scale,
                        "visual_tags": shot.visual_tags,
                        "keyframe_sha256": shot.keyframe_sha256,
                        "coverage": evidence.coverage,
                        "rights_granted": False,
                    }
                )
    for item_id, source in sources.items():
        if _source(channel_id, project_id, item_id) != source:
            raise EditorialConflict("Permission or source changed during search")
    return results[: request.limit]


def reconcile_project_scene_index(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    confirmed_worker_stopped: bool,
    actor: str,
) -> dict[str, Any]:
    source = _source(channel_id, project_id, item_id)
    if not confirmed_worker_stopped:
        raise EditorialConflict("Confirm the indexing worker has stopped before reconciling")
    with session_scope() as session:
        row = session.get(MediaSceneIndex, _identity(channel_id, source))
        if row is None or row.status != "indexing":
            raise EditorialConflict("There is no unfinished indexing attempt to reconcile")
        started = datetime.fromisoformat(row.receipt["started_at"])
        if (datetime.now(UTC) - started).total_seconds() <= WALL_SECONDS:
            raise EditorialConflict("Wait for the bounded decoder window before reconciling")
        receipt = {
            **row.receipt,
            "reconciled_by": actor,
            "reconciled_at": datetime.now(UTC).isoformat(),
        }
        changed = session.scalar(
            update(MediaSceneIndex)
            .where(
                MediaSceneIndex.id == row.id,
                MediaSceneIndex.status == "indexing",
                MediaSceneIndex.receipt["lease_token"].as_string()
                == row.receipt.get("lease_token"),
            )
            .values(
                status="failed",
                error_category="interrupted_worker_confirmed_stopped",
                receipt=receipt,
            )
            .returning(MediaSceneIndex.id)
        )
        if changed is None:
            raise EditorialConflict("Indexing changed; refresh its current receipt")
    return get_project_scene_index(channel_id, project_id, item_id, verify_bytes=False)
