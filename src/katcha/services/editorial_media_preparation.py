"""Resumable script-to-Bin composition over existing parser, ledger and Bin.

Each HTTP continuation executes a finite batch. PostgreSQL stores the plan and
checkpoints in EditorialRun; the existing authoritative search ledger retains
provider evidence across crashes. No media bytes are downloaded here.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from katcha.acquisition.adapters import DiscoveryAdapter, get_adapter
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.editorial.media_bin_schemas import ProjectMediaItemResponse
from katcha.editorial.media_preparation_schemas import (
    ReconcileMediaPreparation,
    StartMediaPreparation,
)
from katcha.editorial.project_schemas import (
    EditorialDraft,
    EditorialScriptSeed,
    SaveEditorialDraft,
)
from katcha.editorial.source_intelligence.candidate_ranker import (
    SourceCoverageReportV1,
    rank_source_coverage,
)
from katcha.editorial.source_intelligence.contracts import ProviderPolicyV1, SourceSearchJobV1
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability, search_capability
from katcha.editorial.source_intelligence.query_expansion import ProviderRoute, expand_queries
from katcha.editorial.source_intelligence.requirements import AssetManifestV1, build_asset_manifest
from katcha.editorial.source_intelligence.scouting import ScoutPageV1, ScoutRequest, scout
from katcha.editorial_models import EditorialProject, EditorialRevision, EditorialRun
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_media_bin import add_project_media_item, list_project_media_items
from katcha.services.editorial_projects import (
    EditorialConflict,
    EditorialNotFound,
    _digest,
    get_project,
    save_draft_in_session,
)

TARGET = "prepare_media"
LEASE_SECONDS = 600


@dataclass(frozen=True)
class PreparationRuntime:
    """Trusted server composition; never accepted from an HTTP request."""

    ledger: SearchJobLedger
    routes: tuple[ProviderRoute, ...]
    adapters: dict[str, DiscoveryAdapter]
    max_jobs: int = 48
    batch_size: int = 5


@lru_cache(maxsize=1)
def runtime() -> PreparationRuntime:
    adapters = {key: get_adapter(key, "v1") for key in ("youtube", "reddit")}
    policy = ProviderPolicyV1(
        version="project-media-preparation-v1",
        max_calls=24,
        max_results=240,
        external_search_authorized=False,
    )
    return PreparationRuntime(
        ledger=SearchJobLedger(get_settings().work_dir / "source-search-ledger.sqlite"),
        routes=tuple(
            ProviderRoute(search_capability(adapter), policy, "public_reference", 10)
            for adapter in adapters.values()
        ),
        adapters=adapters,
    )


def _event(session: Session, row: EditorialRun) -> None:
    session.add(
        DomainEvent(
            aggregate_type="editorial_project",
            aggregate_id=str(row.project_id),
            event_type="editorial.media_preparation_checkpoint",
            payload={
                "channel_profile_id": str(row.channel_profile_id),
                "project_id": str(row.project_id),
                "run_id": str(row.id),
                "revision": row.input_revision,
                "status": row.status,
                "stage": row.stage,
                "completed_jobs": len(row.artifacts.get("completed_job_ids", [])),
                "total_jobs": len(row.artifacts.get("jobs", [])),
                "actor": row.actor,
            },
        )
    )


def _seed(
    project: EditorialProject, revision: EditorialRevision | None
) -> tuple[EditorialScriptSeed, str]:
    # Reuse the exact-script parser for saved beats as well as imported scripts.
    # The stored draft and original import remain untouched; this is a lossless
    # direction/narration envelope, not another parser or an AI rewrite.
    beats = (revision.draft if revision else {}).get("script", [])
    if beats:
        text = "\n\n".join(
            f"{beat['narration']}\n[VISUAL: {beat['visual_intent']}]" for beat in beats
        )
        if len(text) > 120_000:
            raise EditorialConflict(
                "This saved script exceeds the 120,000-character media planning limit. "
                "Prepare a shorter saved script; the current script is preserved."
            )
        return EditorialScriptSeed(
            text=text,
            origin="operator_paste",
            content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        ), "saved_revision"
    imported = project.brief.get("script_seed")
    if not imported:
        raise EditorialConflict(
            "Import a finished script or save script beats before finding media"
        )
    return EditorialScriptSeed.model_validate(imported), "imported_script"


def _lock_project(
    session: Session, channel_id: uuid.UUID, project_id: uuid.UUID
) -> EditorialProject:
    result = session.execute(
        update(EditorialProject)
        .where(
            EditorialProject.id == project_id,
            EditorialProject.channel_profile_id == channel_id,
        )
        .values(revision=EditorialProject.revision, updated_at=EditorialProject.updated_at)
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        raise EditorialNotFound("Editorial project not found in this channel")
    project = session.get(EditorialProject, project_id)
    assert project is not None
    session.refresh(project)
    return project


def start_preparation(
    channel_id: uuid.UUID, project_id: uuid.UUID, request: StartMediaPreparation, *, actor: str
) -> EditorialRun:
    run_id = uuid.uuid5(project_id, f"media-preparation:{request.idempotency_key}")
    request_digest = _digest(request.model_dump(mode="json"))
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        project = _lock_project(session, channel_id, project_id)
        previous = session.get(EditorialRun, run_id)
        if previous:
            if previous.channel_profile_id != channel_id or previous.input_digest != request_digest:
                raise EditorialConflict("Preparation identity was used for different options")
            session.expunge(previous)
            return previous
        if project.revision != request.expected_revision:
            raise EditorialConflict("Script changed; reload before finding media")
        # A different browser/request identity must not start redundant work.
        previous = session.scalar(
            select(EditorialRun)
            .where(
                EditorialRun.project_id == project_id,
                EditorialRun.channel_profile_id == channel_id,
                EditorialRun.input_revision == project.revision,
                EditorialRun.options["target"].as_string() == TARGET,
                EditorialRun.options["external_search_authorized"].as_boolean()
                == request.external_search_authorized,
            )
            .order_by(EditorialRun.created_at.desc())
            .limit(1)
        )
        if previous:
            session.expunge(previous)
            return previous
        active = session.scalar(
            select(EditorialRun.id)
            .where(
                EditorialRun.project_id == project_id,
                EditorialRun.channel_profile_id == channel_id,
                EditorialRun.input_revision == project.revision,
                EditorialRun.options["target"].as_string() == TARGET,
                EditorialRun.status != "media_completed",
            )
            .limit(1)
        )
        if active is not None:
            raise EditorialConflict(
                "Finish the saved media preparation before changing search permission"
            )
        revision = session.get(EditorialRevision, (project_id, project.revision))
        seed, source = _seed(project, revision)
        if project.revision == 0:
            revision = save_draft_in_session(
                session,
                channel_id,
                project_id,
                SaveEditorialDraft(
                    expected_revision=0,
                    idempotency_key=f"media-preparation-{run_id}",
                    draft=EditorialDraft(),
                ),
                actor=actor,
            )
            session.refresh(project)
        assert revision is not None
        configured = runtime()
        if not 1 <= configured.max_jobs <= 1000 or not 1 <= configured.batch_size <= 20:
            raise ValueError("Media preparation requires bounded server configuration")
        routes = tuple(
            replace(
                route,
                policy=route.policy.model_copy(
                    update={
                        "external_search_authorized": request.external_search_authorized,
                    }
                ),
            )
            for route in configured.routes
        )
        manifest = build_asset_manifest(
            seed,
            channel_id=channel_id,
            project_id=project_id,
            approved_source_classes=("owned", "public_reference"),
        )
        old_volume = session.scalar(
            select(EditorialRun.id)
            .where(
                EditorialRun.project_id == project_id,
                EditorialRun.channel_profile_id == channel_id,
                EditorialRun.options["target"].as_string() == TARGET,
                EditorialRun.artifacts["manifest"]["scope"]["revision_digest"].as_string()
                == manifest.scope.revision_digest,
                EditorialRun.artifacts["ledger_id"].as_string() != configured.ledger.identity,
            )
            .limit(1)
        )
        if old_volume is not None:
            raise EditorialConflict(
                "Restore this script's saved search evidence volume before starting preparation"
            )
        expansion = expand_queries(
            manifest,
            routes=routes,
            requested_by="project-media-preparation",
            max_jobs=configured.max_jobs,
        )
        row = EditorialRun(
            id=run_id,
            project_id=project_id,
            channel_profile_id=channel_id,
            input_revision=project.revision,
            input_digest=request_digest,
            actor=actor,
            options={"target": TARGET, **request.model_dump(mode="json")},
            status="media_pending",
            stage="media_plan_saved",
            artifacts={
                "schema_version": 1,
                "manifest": manifest.model_dump(mode="json"),
                "script_source": source,
                "input_draft_digest": revision.digest,
                "source_clip_bindings": project.brief.get("source_clip_bindings", {}),
                "routes": [
                    {
                        "capability": asdict(route.capability),
                        "policy": route.policy.model_dump(mode="json"),
                        "source_class": route.source_class,
                        "max_results": route.max_results,
                    }
                    for route in routes
                ],
                "max_jobs": configured.max_jobs,
                "ledger_id": configured.ledger.identity,
                "batch_size": configured.batch_size,
                "jobs": [job.model_dump(mode="json") for job in expansion.jobs],
                "planning_gaps": [
                    {"requirement_id": item.requirement_id, **gap.model_dump(mode="json")}
                    for item in expansion.requirements
                    for gap in item.gaps
                ],
                "completed_job_ids": [],
                "coverage": None,
            },
        )
        session.add(row)
        session.flush()
        _event(session, row)
        session.expunge(row)
        return row


def get_preparation(
    channel_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> EditorialRun:
    # Check scope before opening the shared search ledger or returning artifacts.
    with session_scope() as session:
        row = session.scalar(
            select(EditorialRun).where(
                EditorialRun.id == run_id,
                EditorialRun.channel_profile_id == channel_id,
                EditorialRun.project_id == project_id,
                EditorialRun.options["target"].as_string() == TARGET,
            )
        )
        if row is None:
            raise EditorialNotFound("Media preparation not found in this project")
        session.expunge(row)
        return row


def latest_preparation(channel_id: uuid.UUID, project_id: uuid.UUID) -> EditorialRun | None:
    get_project(channel_id, project_id)
    with session_scope() as session:
        row = session.scalar(
            select(EditorialRun)
            .where(
                EditorialRun.channel_profile_id == channel_id,
                EditorialRun.project_id == project_id,
                EditorialRun.options["target"].as_string() == TARGET,
            )
            .order_by(EditorialRun.created_at.desc(), EditorialRun.id)
            .limit(1)
        )
        if row:
            session.expunge(row)
        return row


def _pages(
    ledger: SearchJobLedger, jobs: list[SourceSearchJobV1], manifest: AssetManifestV1
) -> tuple[ScoutPageV1, ...]:
    pages = []
    for job in jobs:
        receipt = ledger.read(job.job_id, scope=manifest.scope)
        if receipt is not None:
            pages.append(
                ScoutPageV1(
                    receipt=receipt, candidates=ledger.candidates(job.job_id, scope=manifest.scope)
                )
            )
    return tuple(pages)


def _publish(row: EditorialRun, report: SourceCoverageReportV1) -> None:
    """Exact replay keys and immutable provenance preserve operator Bin items."""
    grouped: dict[str, dict[str, Any]] = {}
    for coverage in report.requirements:
        for reference in coverage.references:
            group = grouped.setdefault(reference.canonical_url, {"requirements": [], "refs": []})
            group["requirements"].append(coverage.requirement_id)
            group["refs"].append(reference)
    kinds = {
        item["requirement_id"]: item["kind"] for item in row.artifacts["manifest"]["requirements"]
    }
    candidates = {
        candidate.candidate_id: candidate
        for value in row.artifacts["jobs"]
        for candidate in runtime().ledger.candidates(value["job_id"], scope=report.scope)
    }
    existing = list_project_media_items(
        row.channel_profile_id, row.project_id, revision=row.input_revision
    )
    by_url = {item.external_reference_url: item for item in existing if item.external_reference_url}
    by_clip = {str(item.clip_id): item for item in existing if item.clip_id}
    bindings = []
    for url, group in sorted(grouped.items()):
        requirements = sorted(set(group["requirements"]))
        refs = group["refs"]
        selected = sorted({key for ref in refs for key in ref.supporting_candidate_ids})
        observations = [
            candidates[key].observed_metadata.model_dump(mode="json")
            for key in selected
            if key in candidates
        ]
        source_types = {candidates[key].source_type for key in selected if key in candidates}
        provenance = {
            "preparation_run_id": str(row.id),
            "revision_digest": report.scope.revision_digest,
            "scope": report.scope.model_dump(mode="json"),
            "candidate_ids": selected,
            "scout_receipt_refs": sorted({key for ref in refs for key in ref.scout_receipt_refs}),
            "observation_receipt_refs": sorted(
                {key for ref in refs for key in ref.observation_receipt_refs}
            ),
            "providers": sorted({key for ref in refs for key in ref.supporting_providers}),
            "basis": "provider_reported_reference",
        }
        if url in by_url:
            # Preserve existing operator rights/notes and avoid another Bin row.
            # New requirement/evidence bindings remain in this durable run.
            bindings.append(
                {
                    "media_item_id": str(by_url[url].id),
                    "requirement_ids": requirements,
                    "provenance": provenance,
                }
            )
            continue
        item = add_project_media_item(
            row.channel_profile_id,
            row.project_id,
            row.input_revision,
            request_key=f"prepared-{row.id}-{hashlib.sha256(url.encode()).hexdigest()}",
            actor=row.actor,
            kind="video"
            if source_types == {"video_reference"}
            else "citation"
            if all(kinds[key] == "citation" for key in requirements)
            else "unknown",
            external_reference_url=url,
            requirement_ids=requirements,
            status="reference_only",
            rights_status="not_assessed",
            provenance=provenance,
            source_metadata={
                "availability": "reference_only",
                "publisher_authority": "unverified",
                "observations": observations,
            },
        )
        bindings.append(
            {
                "media_item_id": str(item.id),
                "requirement_ids": requirements,
                "provenance": provenance,
            }
        )
    # Existing project bindings already passed channel membership validation.
    # Bin service rechecks it, references the canonical Clip, and never copies bytes.
    owned_bindings: dict[str, list[str]] = {}
    for url, clip_id in row.artifacts["source_clip_bindings"].items():
        owned_bindings.setdefault(clip_id, []).append(url)
    for clip_id, urls in sorted(owned_bindings.items()):
        if clip_id in by_clip:
            bindings.append(
                {
                    "media_item_id": str(by_clip[clip_id].id),
                    "requirement_ids": [],
                    "provenance": {
                        "source_urls": sorted(urls),
                        "basis": "existing_project_clip_binding",
                    },
                }
            )
            continue
        item = add_project_media_item(
            row.channel_profile_id,
            row.project_id,
            row.input_revision,
            request_key=f"prepared-{row.id}-owned-{clip_id}",
            actor=row.actor,
            kind="video",
            clip_id=uuid.UUID(clip_id),
            status="acquired",
            rights_status="not_assessed",
            provenance={
                "preparation_run_id": str(row.id),
                "source_urls": sorted(urls),
                "basis": "existing_project_clip_binding",
            },
        )
        bindings.append(
            {"media_item_id": str(item.id), "requirement_ids": [], "provenance": item.provenance}
        )
    row.artifacts = {**row.artifacts, "bin_bindings": bindings}


def resume_preparation(
    channel_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
) -> EditorialRun:
    token = str(uuid.uuid4())
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        project = _lock_project(session, channel_id, project_id)
        row = session.get(EditorialRun, run_id)
        if (
            row is None
            or (row.channel_profile_id, row.project_id) != (channel_id, project_id)
            or row.options.get("target") != TARGET
        ):
            raise EditorialNotFound("Media preparation not found in this project")
        if row.status == "media_completed":
            session.expunge(row)
            return row
        revision = session.get(EditorialRevision, (project_id, project.revision))
        if (
            project.revision != row.input_revision
            or revision is None
            or revision.digest != row.artifacts["input_draft_digest"]
        ):
            raise EditorialConflict("Script changed; start preparation for the current revision")
        lease = row.artifacts.get("lease_until")
        if (
            row.status == "media_running"
            and lease
            and datetime.fromisoformat(lease) > datetime.now(UTC)
        ):
            session.expunge(row)
            return row
        row.status = "media_running"
        row.stage = "media_scouting"
        row.error = None
        row.artifacts = {
            **row.artifacts,
            "lease_token": token,
            "lease_until": (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
        }
        _event(session, row)
        session.flush()
        session.expunge(row)
    try:
        configured = runtime()
        if configured.ledger.identity != row.artifacts["ledger_id"]:
            raise EditorialConflict(
                "Search evidence volume changed; restore the saved ledger before continuing"
            )
        manifest = AssetManifestV1.model_validate(row.artifacts["manifest"])
        routes = {
            item["capability"]["provider_key"]: ProviderRoute(
                SearchCapability(**item["capability"]),
                ProviderPolicyV1.model_validate(item["policy"]),
                item["source_class"],
                item["max_results"],
            )
            for item in row.artifacts["routes"]
        }
        jobs = [SourceSearchJobV1.model_validate(item) for item in row.artifacts["jobs"]]
        completed = set(row.artifacts["completed_job_ids"])
        pending = [job for job in jobs if job.job_id not in completed]
        # A stopped/slow worker's reservation must not starve independent jobs.
        pending.sort(
            key=lambda job: bool(
                (saved := configured.ledger.read(job.job_id, scope=manifest.scope))
                and saved.status == "running"
            )
        )
        selected = pending[: row.artifacts["batch_size"]]
        for job in selected:
            _assert_current(row, token)
            route = routes[job.provider_key]
            adapter = configured.adapters.get(job.provider_key)
            if adapter is None or (adapter.key, adapter.version) != (
                job.provider_key,
                job.provider_version,
            ):
                raise ValueError("Saved provider is unavailable; restore its server configuration")
            page = scout(
                configured.ledger,
                (ScoutRequest(job, route.policy, adapter, route.capability),),
                scope=manifest.scope,
            )[0]
            if page.receipt.status != "running":
                completed.add(job.job_id)
            if page.continuation and page.continuation.job_id not in {item.job_id for item in jobs}:
                if len(jobs) < row.artifacts["max_jobs"]:
                    jobs.append(page.continuation)
                else:
                    row.artifacts["planning_gaps"] = [
                        *row.artifacts["planning_gaps"],
                        {
                            "requirement_id": job.requirement_id,
                            "category": "pagination_budget_exhausted",
                            "next_action": (
                                "Inspect saved references; additional pages exceed this limit."
                            ),
                        },
                    ]
        pages = _pages(configured.ledger, jobs, manifest)
        report = rank_source_coverage(
            manifest, ledger=configured.ledger, jobs=tuple(jobs), pages=pages
        )
        done = len(completed) == len(jobs)
        running = [page for page in pages if page.receipt.status == "running"]
        if done:
            _assert_current(row, token)
            row.artifacts = {**row.artifacts, "jobs": [job.model_dump(mode="json") for job in jobs]}
            _publish(row, report)
        artifacts = {
            **row.artifacts,
            "jobs": [job.model_dump(mode="json") for job in jobs],
            "completed_job_ids": sorted(completed),
            "coverage": report.model_dump(mode="json"),
            "search_receipts": [
                page.receipt.model_dump(mode="json", exclude={"next_cursor"}) for page in pages
            ],
            "lease_until": None,
            "lease_token": None,
            "recovery_available_at": max(
                (
                    datetime.fromisoformat(page.receipt.started_at)
                    + timedelta(seconds=LEASE_SECONDS)
                ).isoformat()
                for page in running
            )
            if running
            else None,
        }
        with session_scope() as session:
            result = session.execute(
                update(EditorialRun)
                .where(
                    EditorialRun.id == run_id,
                    EditorialRun.artifacts["lease_token"].as_string() == token,
                )
                .values(
                    artifacts=artifacts,
                    status="media_completed"
                    if done
                    else "media_failed"
                    if running
                    else "media_pending",
                    stage="media_bin_saved"
                    if done
                    else "media_outcome_unknown"
                    if running
                    else "media_scouting",
                    error=(
                        "A previous search is still running or its outcome is unknown. "
                        "Refresh saved work, or confirm it has stopped before recording "
                        "an unknown outcome. The search will not be repeated."
                    )
                    if running
                    else None,
                )
            )
            if cast(CursorResult[Any], result).rowcount == 1:
                current = session.get(EditorialRun, run_id)
                assert current is not None
                _event(session, current)
    except Exception:
        # The receipt ledger, not a retry, decides any provider outcome. An
        # exception after a saved page or Bin insert can safely replay both.
        with session_scope() as session:
            session.execute(
                update(EditorialRun)
                .where(
                    EditorialRun.id == run_id,
                    EditorialRun.artifacts["lease_token"].as_string() == token,
                )
                .values(
                    status="media_failed",
                    stage="media_checkpoint_needed",
                    error=(
                        "Preparation paused. Continue saved work; "
                        "searches with unknown outcomes are not repeated."
                    ),
                    artifacts={**row.artifacts, "lease_until": None, "lease_token": None},
                )
            )
        raise
    return get_preparation(channel_id, project_id, run_id)


def _assert_current(row: EditorialRun, token: str) -> None:
    with session_scope() as session:
        ensure_active_profile(session, row.channel_profile_id)
        current = session.get(EditorialRun, row.id)
        project = session.get(EditorialProject, row.project_id)
        if (
            current is None
            or project is None
            or current.artifacts.get("lease_token") != token
            or project.revision != row.input_revision
        ):
            raise EditorialConflict("Preparation or script changed; reload its saved progress")


def reconcile_preparation(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: ReconcileMediaPreparation,
) -> EditorialRun:
    """Explicit stopped-worker reconciliation; no retry or new reservation."""
    if not request.confirmed_worker_stopped:
        raise EditorialConflict("Confirm that the previous search has stopped")
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        _lock_project(session, channel_id, project_id)
        row = session.get(EditorialRun, run_id)
        if (
            row is None
            or (row.channel_profile_id, row.project_id) != (channel_id, project_id)
            or row.options.get("target") != TARGET
        ):
            raise EditorialNotFound("Media preparation not found in this project")
        configured = runtime()
        if configured.ledger.identity != row.artifacts["ledger_id"]:
            raise EditorialConflict("Restore the saved search ledger before reconciling")
        manifest = AssetManifestV1.model_validate(row.artifacts["manifest"])
        jobs = [SourceSearchJobV1.model_validate(item) for item in row.artifacts["jobs"]]
        pages = _pages(configured.ledger, jobs, manifest)
        running = [page for page in pages if page.receipt.status == "running"]
        for page in running:
            deadline = datetime.fromisoformat(page.receipt.started_at) + timedelta(
                seconds=LEASE_SECONDS
            )
            if deadline > datetime.now(UTC):
                raise EditorialConflict("Wait for the search recovery window before reconciling")
        for page in running:
            configured.ledger.mark_interrupted(page.receipt.job_id, scope=manifest.scope)
        if running:
            row.status = "media_pending"
            row.stage = "media_outcome_reconciled"
            row.error = None
            row.artifacts = {
                **row.artifacts,
                "lease_token": None,
                "lease_until": None,
                "recovery_available_at": None,
            }
            _event(session, row)
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def response(row: EditorialRun) -> dict[str, Any]:
    project = get_project(row.channel_profile_id, row.project_id)
    return {
        "schema_version": 1,
        "run_id": str(row.id),
        "project_id": str(row.project_id),
        "channel_profile_id": str(row.channel_profile_id),
        "input_revision": row.input_revision,
        "external_search_authorized": row.options["external_search_authorized"],
        "status": row.status.removeprefix("media_"),
        "stage": row.stage,
        "error": row.error,
        "stale": project.revision != row.input_revision,
        "progress": {
            "completed_jobs": len(row.artifacts["completed_job_ids"]),
            "total_jobs": len(row.artifacts["jobs"]),
        },
        "manifest": row.artifacts["manifest"],
        "coverage": row.artifacts["coverage"],
        "planning_gaps": row.artifacts["planning_gaps"],
        "bin_bindings": row.artifacts.get("bin_bindings", []),
        "search_receipts": row.artifacts.get("search_receipts", []),
        "recovery_available_at": row.artifacts.get("recovery_available_at"),
        "recovery_required": row.stage == "media_outcome_unknown",
        "media_bin_items": [
            ProjectMediaItemResponse.model_validate(item).model_dump(mode="json")
            for item in list_project_media_items(
                row.channel_profile_id, row.project_id, revision=row.input_revision
            )
        ],
        "updated_at": row.updated_at,
    }
