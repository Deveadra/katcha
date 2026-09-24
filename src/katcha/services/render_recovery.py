from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.domain import ProductionStatus
from katcha.edit_render_models import RenderAttempt
from katcha.models import DomainEvent
from katcha.production_models import Production, ProductionAsset
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset

RenderSourceKind = Literal["production", "short_episode"]
_TERMINAL = {"succeeded", "failed", "dead_letter"}


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_model(source_kind: RenderSourceKind):
    return Production if source_kind == "production" else ShortEpisode


def _source_filter(source_kind: RenderSourceKind, source_id: uuid.UUID):
    if source_kind == "production":
        return RenderAttempt.production_id == source_id
    return RenderAttempt.short_episode_id == source_id


def _source_kwargs(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> dict[str, uuid.UUID | None]:
    return {
        "production_id": source_id if source_kind == "production" else None,
        "short_episode_id": source_id if source_kind == "short_episode" else None,
    }


def _locked_source(session: object, source_kind: RenderSourceKind, source_id: uuid.UUID):
    model = _source_model(source_kind)
    source = session.scalar(
        select(model).where(model.id == source_id).with_for_update()
    )
    if source is None:
        raise ValueError(f"{source_kind.replace('_', ' ')} not found: {source_id}")
    return source


def _latest_attempt_in_session(
    session: object,
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> RenderAttempt | None:
    return session.scalar(
        select(RenderAttempt)
        .where(_source_filter(source_kind, source_id))
        .order_by(RenderAttempt.attempt_number.desc())
        .limit(1)
    )


def register_render_attempt(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
    *,
    workflow_id: str | None = None,
    requested_by: str = "workflow",
) -> RenderAttempt:
    with session_scope() as session:
        if workflow_id:
            existing = session.scalar(
                select(RenderAttempt).where(RenderAttempt.workflow_id == workflow_id)
            )
            if existing is not None:
                session.expunge(existing)
                return existing

        source = _locked_source(session, source_kind, source_id)
        max_attempt = int(
            session.scalar(
                select(func.max(RenderAttempt.attempt_number)).where(
                    _source_filter(source_kind, source_id)
                )
            )
            or 0
        )
        attempt_number = max_attempt + 1
        latest = _latest_attempt_in_session(session, source_kind, source_id)
        resolved_workflow_id = (
            workflow_id
            or f"render-retry-{source_kind}-{source_id}-a{attempt_number}"
        )
        row = RenderAttempt(
            **_source_kwargs(source_kind, source_id),
            retry_of_id=latest.id if latest is not None else None,
            attempt_number=attempt_number,
            workflow_id=resolved_workflow_id,
            status="queued",
            requested_by=requested_by[:128],
            attempt_metadata={
                "source_workflow_id": str(source.workflow_id),
                "source_generation": int(getattr(source, "generation", 1) or 1),
                "brand_key": getattr(source, "brand_key", None),
                "brand_version": getattr(source, "brand_version", None),
                "edit_blueprint_key": getattr(source, "edit_blueprint_key", None),
                "edit_blueprint_version": getattr(source, "edit_blueprint_version", None),
            },
        )
        session.add(row)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=str(source_id),
                event_type=f"{source_kind}.render_attempt_created",
                payload={
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "render_attempt_id": str(row.id),
                    "attempt_number": attempt_number,
                    "workflow_id": resolved_workflow_id,
                    "requested_by": requested_by[:128],
                    "retry_of_id": str(row.retry_of_id) if row.retry_of_id else None,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def begin_render_attempt(attempt_id: uuid.UUID) -> RenderAttempt:
    with session_scope() as session:
        row = session.scalar(
            select(RenderAttempt)
            .where(RenderAttempt.id == attempt_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError(f"render attempt not found: {attempt_id}")
        if row.status == "queued":
            row.status = "running"
            row.started_at = datetime.now(UTC)
        elif row.status in _TERMINAL:
            session.expunge(row)
            return row
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def bind_render_manifest(
    attempt_id: uuid.UUID,
    manifest: dict[str, Any],
) -> RenderAttempt:
    if not manifest:
        raise ValueError("render attempt cannot bind an empty manifest")
    with session_scope() as session:
        row = session.scalar(
            select(RenderAttempt)
            .where(RenderAttempt.id == attempt_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError(f"render attempt not found: {attempt_id}")
        digest = _canonical_sha256(manifest)
        if row.manifest_sha256 and row.manifest_sha256 != digest:
            raise ValueError("render attempt is already bound to another manifest")
        row.manifest_snapshot = dict(manifest)
        row.manifest_sha256 = digest
        row.manifest_version = str(manifest.get("version") or "") or None
        row.output_key = str(manifest.get("output_key") or "") or None
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def record_pre_render_qc(
    attempt_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    with session_scope() as session:
        row = session.get(RenderAttempt, attempt_id)
        if row is None:
            raise ValueError(f"render attempt not found: {attempt_id}")
        if row.status in _TERMINAL:
            return
        row.pre_qc = dict(payload)


def record_post_render_success(
    attempt_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    output_key: str,
) -> None:
    with session_scope() as session:
        row = session.scalar(
            select(RenderAttempt)
            .where(RenderAttempt.id == attempt_id)
            .with_for_update()
        )
        if row is None:
            raise ValueError(f"render attempt not found: {attempt_id}")
        if row.status == "succeeded":
            return
        if row.status in {"failed", "dead_letter"}:
            raise ValueError("failed render attempt cannot be marked successful")
        row.status = "succeeded"
        row.post_qc = dict(payload)
        row.output_key = output_key
        row.error = None
        row.failure_kind = None
        row.completed_at = datetime.now(UTC)
        source_kind: RenderSourceKind = (
            "production" if row.production_id is not None else "short_episode"
        )
        source_id = row.production_id or row.short_episode_id
        if source_id is None:
            raise RuntimeError("render attempt has no source")
        source = _locked_source(session, source_kind, source_id)
        if source_kind == "production":
            source.status = ProductionStatus.REVIEW.value
            source.stage = "render_qc_passed"
        else:
            source.status = "rendered"
            source.stage = "render_review"
        source.error = None
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=str(source_id),
                event_type=f"{source_kind}.render_attempt_succeeded",
                payload={
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "render_attempt_id": str(row.id),
                    "attempt_number": row.attempt_number,
                    "output_key": output_key,
                    "qc": payload,
                },
            )
        )


def dead_letter_render_attempt(
    attempt_id: uuid.UUID,
    message: str,
    *,
    failure_kind: str = "render_failure",
) -> None:
    with session_scope() as session:
        row = session.scalar(
            select(RenderAttempt)
            .where(RenderAttempt.id == attempt_id)
            .with_for_update()
        )
        if row is None:
            return
        if row.status == "succeeded":
            return
        if row.status == "dead_letter":
            return
        row.status = "dead_letter"
        row.failure_kind = failure_kind[:64]
        row.error = message[:8000]
        row.completed_at = datetime.now(UTC)
        source_kind: RenderSourceKind = (
            "production" if row.production_id is not None else "short_episode"
        )
        source_id = row.production_id or row.short_episode_id
        if source_id is None:
            return
        source = _locked_source(session, source_kind, source_id)
        if source_kind == "production":
            source.status = ProductionStatus.FAILED.value
        else:
            source.status = "failed"
        source.stage = "render_dead_letter"
        source.error = row.error
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=str(source_id),
                event_type=f"{source_kind}.render_dead_lettered",
                payload={
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "render_attempt_id": str(row.id),
                    "attempt_number": row.attempt_number,
                    "failure_kind": row.failure_kind,
                    "error": row.error[:1000] if row.error else None,
                },
            )
        )


def reserve_render_retry(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
    *,
    actor: str = "operator",
) -> RenderAttempt:
    with session_scope() as session:
        source = _locked_source(session, source_kind, source_id)
        latest = _latest_attempt_in_session(session, source_kind, source_id)
        if latest is None:
            raise ValueError("source has no prior render attempt to retry")
        if latest.status not in {"failed", "dead_letter"}:
            raise ValueError("latest render attempt is not retryable")
        if source_kind == "production":
            if source.selected_script_id is None:
                raise ValueError("render retry requires a selected production script")
        elif source.selected_script_id is None:
            raise ValueError("render retry requires a selected episode script")

    attempt = register_render_attempt(
        source_kind,
        source_id,
        requested_by=actor,
    )
    with session_scope() as session:
        source = _locked_source(session, source_kind, source_id)
        if source_kind == "production":
            source.status = ProductionStatus.VOICED.value
        else:
            source.status = "editorial_approved"
        source.stage = "render_retry_queued"
        source.error = None
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=str(source_id),
                event_type=f"{source_kind}.render_retry_queued",
                payload={
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "render_attempt_id": str(attempt.id),
                    "attempt_number": attempt.attempt_number,
                    "workflow_id": attempt.workflow_id,
                    "actor": actor[:128],
                },
            )
        )
    return attempt


def list_render_attempts(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> list[RenderAttempt]:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(RenderAttempt)
                .where(_source_filter(source_kind, source_id))
                .order_by(RenderAttempt.attempt_number)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def latest_render_attempt(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> RenderAttempt | None:
    with session_scope() as session:
        row = _latest_attempt_in_session(session, source_kind, source_id)
        if row is not None:
            session.expunge(row)
        return row


def assert_render_verified(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> None:
    with session_scope() as session:
        latest = _latest_attempt_in_session(session, source_kind, source_id)
        if latest is not None:
            qc = dict(latest.post_qc or {})
            if latest.status != "succeeded" or qc.get("status") != "passed":
                raise ValueError("latest render attempt has not passed post-render QC")
            return

        if source_kind == "production":
            asset = session.scalar(
                select(ProductionAsset).where(
                    ProductionAsset.production_id == source_id,
                    ProductionAsset.kind == "render",
                    ProductionAsset.generation == 1,
                )
            )
        else:
            asset = session.scalar(
                select(ShortEpisodeAsset).where(
                    ShortEpisodeAsset.short_episode_id == source_id,
                    ShortEpisodeAsset.kind == "render",
                    ShortEpisodeAsset.generation == 1,
                )
            )
        if asset is None or not bool((asset.asset_metadata or {}).get("verified")):
            raise ValueError("render has not passed deterministic verification")
