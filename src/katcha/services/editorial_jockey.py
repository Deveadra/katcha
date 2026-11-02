"""Operator-driven Jockey indexing and temporal retrieval over canonical Editorial media.

Every chargeable operation is fenced by a committed EditorialRun receipt before
calling TwelveLabs. Unknown outcomes are never retried automatically. Results
remain review-only and are joined to independently measured local shot evidence.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.editorial.jockey_schemas import JockeyAdmission, JockeySearchRequest
from katcha.editorial.media_intake.scene_index import _source, _stage
from katcha.editorial.source_intelligence.contracts import digest
from katcha.editorial_models import EditorialRun
from katcha.integrations.storage import ObjectStore
from katcha.integrations.twelvelabs_jockey import search_candidates
from katcha.services.editorial_media_preparation import _lock_project
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound
from katcha.services.editorial_runs import checkpoint, get_run
from katcha.services.editorial_shot_matching import _manifest, _snapshot

INDEX_TARGET = "jockey_index"
SEARCH_TARGET = "jockey_search"
MAX_UPLOAD_BYTES = 190_000_000


def _admit(channel: uuid.UUID, body: JockeyAdmission, settings: Settings) -> str:
    if not body.external_inspection_authorized or not body.provider_spend_approved:
        raise EditorialConflict("Explicit external inspection and provider-spend approval required")
    if not settings.twelvelabs_jockey_dispatch_enabled:
        raise EditorialConflict("Jockey dispatch is disabled by the operator")
    if not settings.twelvelabs_jockey_api_token:
        raise EditorialConflict("Jockey API token is not configured")
    if not settings.twelvelabs_jockey_api_token.get_secret_value().strip():
        raise EditorialConflict("Jockey API token is empty")
    if settings.twelvelabs_jockey_max_operations_per_project < 1:
        raise EditorialConflict("Jockey project operation budget is disabled")
    store_id = settings.twelvelabs_jockey_knowledge_stores.get(str(channel))
    if not store_id:
        raise EditorialConflict("A dedicated knowledge store is required for this channel")
    return store_id


def _client(settings: Settings, client: Any) -> Any:
    if client is not None:
        return client
    try:
        from twelvelabs import TwelveLabs
    except ImportError:
        raise EditorialConflict("The TwelveLabs SDK is missing from this runtime") from None
    assert settings.twelvelabs_jockey_api_token is not None
    return TwelveLabs(api_key=settings.twelvelabs_jockey_api_token.get_secret_value())


def _identity(project: uuid.UUID, source: Any, store_id: str) -> uuid.UUID:
    return uuid.uuid5(
        project, f"jockey-index:{source.clip_id}:{source.sha256}:{store_id}"
    )


def _receipt(row: EditorialRun) -> dict[str, Any]:
    return {
        "run_id": str(row.id),
        "status": row.status,
        "stage": row.stage,
        "error": row.error,
        "provider_asset_id": row.artifacts.get("provider_asset_id"),
        "provider_item_id": row.artifacts.get("provider_item_id"),
        "provider_status": row.artifacts.get("provider_status"),
        "chargeable_operations_reserved": row.options.get("chargeable_operations", 0),
        "billing_reconciliation_required": True,
        "automatic_retry_allowed": False,
    }


def _reserve(session: Any, project: uuid.UUID, settings: Settings) -> None:
    # The project row is locked by _lock_project before this check.
    rows = session.scalars(
        select(EditorialRun).where(
            EditorialRun.project_id == project,
            EditorialRun.options["target"].as_string().in_([INDEX_TARGET, SEARCH_TARGET]),
        )
    )
    used = sum(int(row.options.get("chargeable_operations", 0)) for row in rows)
    if used >= settings.twelvelabs_jockey_max_operations_per_project:
        raise EditorialConflict(
            "Jockey operation budget exhausted; reconcile provider billing before increasing it"
        )


def get_index(
    channel: uuid.UUID, project: uuid.UUID, item_id: uuid.UUID
) -> dict[str, Any]:
    settings = get_settings()
    source = _source(channel, project, item_id)
    store_id = settings.twelvelabs_jockey_knowledge_stores.get(str(channel))
    if not store_id:
        raise EditorialConflict("No Jockey store configured for this channel")
    run_id = _identity(project, source, store_id)
    run = get_run(channel, project, run_id)
    if run.options.get("target") != INDEX_TARGET:
        raise EditorialNotFound("Jockey index receipt not found")
    if run.artifacts.get("permission_digest") != source.permission_digest:
        raise EditorialConflict("Indexing permission changed; previous provider binding is stale")
    return _receipt(run)


def start_index(
    channel: uuid.UUID,
    project: uuid.UUID,
    item_id: uuid.UUID,
    body: JockeyAdmission,
    *,
    actor: str,
    client: Any = None,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    store_id = _admit(channel, body, settings)
    source = _source(channel, project, item_id)
    if source.declared_size > MAX_UPLOAD_BYTES:
        raise EditorialConflict("Jockey direct upload is limited to 190 MB per video")
    run_id = _identity(project, source, store_id)
    with session_scope() as session:
        current = _lock_project(session, channel, project)
        if current.revision != body.expected_revision:
            raise EditorialConflict("Script changed; refresh the current revision")
        existing = session.get(EditorialRun, run_id)
        if existing is not None:
            if existing.artifacts.get("permission_digest") != source.permission_digest:
                raise EditorialConflict("Existing Jockey receipt has different permission evidence")
            return _receipt(existing)
        _reserve(session, project, settings)
        session.add(
            EditorialRun(
                id=run_id,
                project_id=project,
                channel_profile_id=channel,
                input_digest=digest(
                    {"clip": str(source.clip_id), "sha": source.sha256, "store": store_id}
                ),
                input_revision=body.expected_revision,
                options={
                    "target": INDEX_TARGET,
                    "chargeable_operations": 1,
                    "knowledge_store_id": store_id,
                    "clip_id": str(source.clip_id),
                    "clip_sha256": source.sha256,
                },
                attempt=1,
                status="running",
                stage="asset_upload_submitting",
                actor=actor,
                artifacts={
                    "permission_digest": source.permission_digest,
                    "permission_evidence_refs": list(source.permission_evidence_refs),
                    "provider_charge_status": "unknown_until_reconciled",
                },
            )
        )
    try:
        with tempfile.TemporaryDirectory(prefix="katcha-jockey-") as temporary:
            path = Path(temporary) / "canonical-video.mp4"
            _stage(source, store or ObjectStore(), path, maximum=MAX_UPLOAD_BYTES)
            if _source(channel, project, item_id) != source:
                raise EditorialConflict("Canonical source or inspection permission changed")
            with path.open("rb") as media:
                asset = _client(settings, client).assets.create(method="direct", file=media)
        asset_id = str(asset.id)
        if not 1 <= len(asset_id) <= 128:
            raise ValueError("Provider returned no asset identity")
        checkpoint(
            str(run_id), 1, stage="asset_processing",
            artifacts={"provider_asset_id": asset_id, "provider_status": str(asset.status)},
        )
    except Exception:
        _block(run_id, "asset_upload_outcome_unknown")
        raise EditorialConflict(
            "Jockey upload outcome is unknown. Inspect the saved receipt and provider usage; "
            "do not retry the upload automatically"
        ) from None
    return get_index(channel, project, item_id)


def _block(run_id: uuid.UUID, stage: str) -> None:
    try:
        checkpoint(
            str(run_id), 1, stage=stage, status="blocked",
            error="Provider outcome requires manual reconciliation; automatic retry prohibited",
        )
    except Exception:
        # The original intent was committed; never hide an uncertain external operation.
        pass


def advance_index(
    channel: uuid.UUID,
    project: uuid.UUID,
    item_id: uuid.UUID,
    body: JockeyAdmission,
    *,
    client: Any = None,
) -> dict[str, Any]:
    settings = get_settings()
    store_id = _admit(channel, body, settings)
    source = _source(channel, project, item_id)
    run_id = _identity(project, source, store_id)
    run = get_run(channel, project, run_id)
    if run.options.get("target") != INDEX_TARGET:
        raise EditorialNotFound("Jockey index receipt not found")
    if run.artifacts.get("permission_digest") != source.permission_digest:
        raise EditorialConflict("Indexing permission changed; provider receipt is stale")
    if run.status != "running":
        return _receipt(run)
    if run.stage == "asset_ready":
        # Claim and persist this chargeable call before any remote request.
        with session_scope() as session:
            current = _lock_project(session, channel, project)
            if current.revision != body.expected_revision:
                raise EditorialConflict("Script changed; refresh before indexing")
            locked = session.get(EditorialRun, run_id)
            if locked is None or locked.status != "running" or locked.stage != "asset_ready":
                raise EditorialConflict("Indexing advanced concurrently; reload the receipt")
            _reserve(session, project, settings)
            locked.options = {**locked.options, "chargeable_operations": 2}
            locked.stage = "item_create_submitting"
        try:
            item = _client(settings, client).knowledge_store_items.create(
                knowledge_store_id=store_id,
                asset_id=run.artifacts["provider_asset_id"],
            )
            item_id_remote = str(item.id)
            if not 1 <= len(item_id_remote) <= 128:
                raise ValueError("Provider returned no item identity")
            checkpoint(
                str(run_id), 1, stage="item_processing",
                artifacts={
                    "provider_item_id": item_id_remote,
                    "provider_status": str(item.status),
                },
            )
        except Exception:
            _block(run_id, "item_create_outcome_unknown")
            raise EditorialConflict(
                "Jockey indexing submission is uncertain; reconcile provider usage before retry"
            ) from None
    elif run.stage in {"asset_processing", "item_processing"}:
        try:
            provider = _client(settings, client)
            if run.stage == "asset_processing":
                remote = provider.assets.retrieve(asset_id=run.artifacts["provider_asset_id"])
            else:
                remote = provider.knowledge_store_items.retrieve(
                    knowledge_store_id=store_id,
                    item_id=run.artifacts["provider_item_id"],
                )
            status = str(remote.status)
            if status == "ready":
                checkpoint(
                    str(run_id), 1,
                    stage="asset_ready" if run.stage == "asset_processing" else "item_ready",
                    status="running" if run.stage == "asset_processing" else "completed",
                    artifacts={"provider_status": status},
                )
            elif status == "failed":
                _block(run_id, "provider_processing_failed")
            elif status in {"queued", "pending", "processing"}:
                checkpoint(
                    str(run_id), 1, artifacts={"provider_status": status}
                )
            else:
                raise ValueError("Unrecognized provider status")
        except Exception:
            # Retrieval does not resubmit a chargeable create operation.
            raise EditorialConflict(
                "Jockey status could not be verified; the saved receipt was preserved"
            ) from None
    else:
        raise EditorialConflict(
            "Provider submission outcome is uncertain; reconcile it before any further calls"
        )
    return get_index(channel, project, item_id)


def _measured_candidates(
    response: dict[str, Any], observed: list[dict[str, Any]], requirement_id: str
) -> list[dict[str, Any]]:
    by_clip = {str(row["source"].clip_id): row for row in observed}
    results: list[dict[str, Any]] = []
    for suggestion in response["suggestions"]:
        row = by_clip.get(suggestion["clip_id"])
        if row is None:
            continue
        evidence = row["evidence"]
        scale = evidence.timebase_numerator / evidence.timebase_denominator
        start = suggestion["source_in_seconds_unverified"]
        end = suggestion["source_out_seconds_unverified"]
        duration = evidence.duration_ticks * scale
        item = row["item"]
        if (
            end > duration + 1e-6
            or (item.source_in_seconds is not None and start < item.source_in_seconds)
            or (item.source_out_seconds is not None and end > item.source_out_seconds)
        ):
            continue
        overlaps = []
        for shot in evidence.shots:
            measured_in, measured_out = shot.start_tick * scale, shot.end_tick * scale
            overlap = min(end, measured_out) - max(start, measured_in)
            if overlap > 0:
                overlaps.append((overlap, shot))
        if not overlaps:
            continue
        _, shot = max(overlaps, key=lambda entry: (entry[0], -entry[1].start_tick))
        results.append(
            {
                **suggestion,
                "requirement_id": requirement_id,
                "media_item_id": str(item.id),
                "measured_shot_id": shot.shot_id,
                "measured_source_in_seconds": shot.start_tick * scale,
                "measured_source_out_seconds": shot.end_tick * scale,
                "measured_keyframe_sha256": shot.keyframe_sha256,
                "scene_index_id": row["index"]["index_id"],
                "source_sha256": evidence.clip_sha256,
                "verification_scope": "timestamp_overlap_only_not_semantic_fact",
                "production_eligible": False,
                "human_review_required": True,
            }
        )
    return results


def get_search(
    channel: uuid.UUID, project: uuid.UUID, run_id: uuid.UUID,
    *, store: ObjectStore | None = None,
) -> dict[str, Any]:
    run = get_run(channel, project, run_id)
    if run.options.get("target") != SEARCH_TARGET:
        raise EditorialNotFound("Jockey search receipt not found")
    request = JockeySearchRequest.model_validate(run.options["search_request"])
    manifest = _manifest(channel, project, request.preparation_run_id, request.expected_revision)
    selected = {value.requirement_id for value in manifest.requirements}
    if request.requirement_id not in selected:
        raise EditorialConflict("Jockey search requirement is no longer in the current script")
    _, gaps, stamp = _snapshot(channel, project, request.item_ids, store=store or ObjectStore())
    if gaps or stamp != run.artifacts.get("snapshot_digest"):
        raise EditorialConflict("Jockey search is stale; current footage evidence changed")
    return {
        "run_id": str(run.id),
        "status": run.status,
        "stage": run.stage,
        "error": run.error,
        "result": run.artifacts.get("jockey_result") if run.status == "completed" else None,
        "billing_reconciliation_required": True,
        "automatic_retry_allowed": False,
        "human_review_required": True,
    }


def search_jockey(
    channel: uuid.UUID,
    project: uuid.UUID,
    body: JockeySearchRequest,
    *,
    actor: str,
    client: Any = None,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    store_id = _admit(channel, body, settings)
    manifest = _manifest(channel, project, body.preparation_run_id, body.expected_revision)
    requirements = {value.requirement_id: value for value in manifest.requirements}
    requirement = requirements.get(body.requirement_id)
    if requirement is None or requirement.kind != "footage":
        raise EditorialConflict("Choose a current footage requirement")
    observed, gaps, stamp = _snapshot(
        channel, project, body.item_ids, store=store or ObjectStore()
    )
    if gaps or len(observed) != len(body.item_ids):
        raise EditorialConflict("Every selected video needs current measured index evidence")
    if len({row["source"].clip_id for row in observed}) != len(observed):
        raise EditorialConflict("Select each canonical video only once")
    allowed: dict[str, uuid.UUID] = {}
    for row in observed:
        source = row["source"]
        source_class = (
            "owned"
            if any(ref.startswith("upload:") for ref in source.permission_evidence_refs)
            else "public_reference"
        )
        if source_class not in requirement.approved_source_classes:
            raise EditorialConflict("Footage does not meet this requirement's source policy")
        receipt = get_run(channel, project, _identity(project, source, store_id))
        if (
            receipt.options.get("target") != INDEX_TARGET
            or receipt.status != "completed"
            or receipt.stage != "item_ready"
            or receipt.artifacts.get("permission_digest") != source.permission_digest
            or receipt.options.get("clip_sha256") != source.sha256
        ):
            raise EditorialConflict("Each video must have a current ready Jockey index receipt")
        allowed[receipt.artifacts["provider_item_id"]] = source.clip_id
    run_id = uuid.uuid5(project, f"jockey-search:{body.idempotency_key}")
    request_data = body.model_dump(mode="json")
    request_digest = digest(request_data)
    with session_scope() as session:
        current = _lock_project(session, channel, project)
        if current.revision != body.expected_revision:
            raise EditorialConflict("Script changed; refresh before search")
        existing = session.get(EditorialRun, run_id)
        if existing is not None:
            if existing.input_digest != request_digest:
                raise EditorialConflict("Jockey search key was reused with different inputs")
            return get_search(channel, project, run_id, store=store)
        previous = list(
            session.scalars(
                select(EditorialRun).where(
                    EditorialRun.project_id == project,
                    EditorialRun.channel_profile_id == channel,
                    EditorialRun.options["target"].as_string() == SEARCH_TARGET,
                    EditorialRun.options["snapshot_digest"].as_string() == stamp,
                    EditorialRun.options["requirement_id"].as_string()
                    == body.requirement_id,
                )
            )
        )
        if previous:
            raise EditorialConflict(
                "These video and requirement inputs already have a provider receipt; inspect it"
            )
        _reserve(session, project, settings)
        session.add(
            EditorialRun(
                id=run_id,
                project_id=project,
                channel_profile_id=channel,
                input_digest=request_digest,
                input_revision=body.expected_revision,
                options={
                    "target": SEARCH_TARGET,
                    "chargeable_operations": 1,
                    "knowledge_store_id": store_id,
                    "search_request": request_data,
                    "snapshot_digest": stamp,
                    "requirement_id": body.requirement_id,
                },
                status="running",
                stage="search_submitting",
                attempt=1,
                actor=actor,
                artifacts={
                    "snapshot_digest": stamp,
                    "provider_charge_status": "unknown_until_reconciled",
                    "allowed_provider_item_count": len(allowed),
                },
            )
        )
    try:
        raw = search_candidates(
            channel_id=channel,
            query=requirement.intent,
            allowed_items=allowed,
            external_inspection_authorized=True,
            settings=settings,
            client=_client(settings, client),
            max_results=body.max_results,
        )
        _, fresh_gaps, fresh_stamp = _snapshot(
            channel, project, body.item_ids, store=store or ObjectStore()
        )
        if fresh_gaps or fresh_stamp != stamp:
            raise EditorialConflict("Footage or rights changed during provider inspection")
        result = {
            "schema_version": "JockeyMeasuredReviewV1",
            "requirement_id": body.requirement_id,
            "coverage": "provider_semantic_hypotheses_measured_time_overlap",
            "suggestions": _measured_candidates(raw, observed, body.requirement_id),
            "provider_ignored": raw["ignored_unmapped_or_invalid"],
            "verified_fact": False,
            "production_eligible": False,
            "rights_granted": False,
            "human_review_required": True,
        }
        checkpoint(
            str(run_id), 1, status="completed", stage="semantic_review_required",
            artifacts={"jockey_result": result},
        )
    except Exception:
        _block(run_id, "search_outcome_unknown")
        raise EditorialConflict(
            "Jockey search outcome is uncertain; inspect saved receipt and provider billing"
        ) from None
    return get_search(channel, project, run_id, store=store)
