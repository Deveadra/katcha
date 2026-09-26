from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.production_models import Production
from katcha.render_models import RenderAttempt
from katcha.short_episode_models import ShortEpisode

RenderSourceKind = Literal["production", "short_episode"]


def _source_clause(source_kind: RenderSourceKind, source_id: uuid.UUID):
    if source_kind == "production":
        return RenderAttempt.production_id == source_id
    return RenderAttempt.short_episode_id == source_id


def _source_details(
    session: Session,
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> tuple[Production | ShortEpisode, uuid.UUID | None]:
    if source_kind == "production":
        source = session.get(Production, source_id)
        if source is None:
            raise ValueError(f"production not found: {source_id}")
        return source, source.parent_production_id
    source = session.get(ShortEpisode, source_id)
    if source is None:
        raise ValueError(f"short episode not found: {source_id}")
    return source, source.parent_episode_id


def _latest_attempt(
    session: Session,
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> RenderAttempt | None:
    return session.scalar(
        select(RenderAttempt)
        .where(_source_clause(source_kind, source_id))
        .order_by(RenderAttempt.attempt_number.desc())
        .limit(1)
    )


def _event(
    *,
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
    event_type: str,
    payload: dict[str, object],
) -> DomainEvent:
    return DomainEvent(
        aggregate_type=source_kind,
        aggregate_id=str(source_id),
        event_type=event_type,
        payload=payload,
    )


def ensure_render_attempt(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
    *,
    output_key: str,
    manifest_version: str,
) -> RenderAttempt:
    with session_scope() as session:
        source, parent_source_id = _source_details(session, source_kind, source_id)
        existing = _latest_attempt(session, source_kind, source_id)
        if existing is not None:
            if existing.output_key != output_key:
                raise RuntimeError("render attempt output key changed within frozen lineage")
            if existing.manifest_version != manifest_version:
                raise RuntimeError("render attempt manifest version changed within frozen lineage")
            if existing.status == "dead_letter":
                raise RuntimeError(
                    "render attempt is dead-lettered; create a render regeneration lineage"
                )
            session.expunge(existing)
            return existing

        parent_attempt_id: uuid.UUID | None = None
        if parent_source_id is not None:
            parent = _latest_attempt(session, source_kind, parent_source_id)
            if parent is not None:
                parent_attempt_id = parent.id

        attempt_number = 1
        attempt_key = (
            f"{source_kind}:{source_id}:g{source.generation}:a{attempt_number}"
        )
        attempt = RenderAttempt(
            attempt_key=attempt_key,
            production_id=source_id if source_kind == "production" else None,
            short_episode_id=source_id if source_kind == "short_episode" else None,
            channel_profile_id=source.channel_profile_id,
            parent_attempt_id=parent_attempt_id,
            source_generation=source.generation,
            attempt_number=attempt_number,
            status="queued",
            stage="queued",
            output_key=output_key,
            manifest_version=manifest_version,
            verification={},
            failure_count=0,
        )
        session.add(attempt)
        session.flush()
        session.add(
            _event(
                source_kind=source_kind,
                source_id=source_id,
                event_type=f"{source_kind}.render_attempt_created",
                payload={
                    "render_attempt_id": str(attempt.id),
                    "attempt_key": attempt.attempt_key,
                    "source_generation": source.generation,
                    "attempt_number": attempt.attempt_number,
                    "parent_attempt_id": (
                        str(parent_attempt_id) if parent_attempt_id else None
                    ),
                    "output_key": output_key,
                    "manifest_version": manifest_version,
                },
            )
        )
        session.refresh(attempt)
        session.expunge(attempt)
        return attempt


def mark_render_attempt_started(attempt_id: uuid.UUID) -> None:
    with session_scope() as session:
        attempt = session.get(RenderAttempt, attempt_id)
        if attempt is None:
            raise ValueError(f"render attempt not found: {attempt_id}")
        if attempt.status == "verified":
            return
        if attempt.status == "dead_letter":
            raise RuntimeError("dead-letter render attempt cannot be restarted")
        attempt.status = "rendering"
        attempt.stage = "rendering"
        attempt.error = None
        if attempt.started_at is None:
            attempt.started_at = datetime.now(UTC)


def mark_render_attempt_retryable_failure(
    attempt_id: uuid.UUID,
    *,
    failure_class: str,
    error: str,
) -> None:
    with session_scope() as session:
        attempt = session.get(RenderAttempt, attempt_id)
        if attempt is None:
            raise ValueError(f"render attempt not found: {attempt_id}")
        if attempt.status in {"verified", "dead_letter"}:
            return
        attempt.failure_count += 1
        attempt.status = "retrying"
        attempt.stage = "retryable_failure"
        attempt.last_failure_class = failure_class[:128]
        attempt.error = error[:8000]
        source_kind: RenderSourceKind = (
            "production" if attempt.production_id is not None else "short_episode"
        )
        source_id = attempt.production_id or attempt.short_episode_id
        if source_id is not None:
            session.add(
                _event(
                    source_kind=source_kind,
                    source_id=source_id,
                    event_type=f"{source_kind}.render_retryable_failure",
                    payload={
                        "render_attempt_id": str(attempt.id),
                        "attempt_number": attempt.attempt_number,
                        "failure_count": attempt.failure_count,
                        "failure_class": attempt.last_failure_class,
                        "error": attempt.error[:1000],
                    },
                )
            )


def mark_render_attempt_verified(
    attempt_id: uuid.UUID,
    *,
    verification: dict[str, object],
) -> None:
    with session_scope() as session:
        attempt = session.get(RenderAttempt, attempt_id)
        if attempt is None:
            raise ValueError(f"render attempt not found: {attempt_id}")
        if attempt.status == "dead_letter":
            raise RuntimeError("dead-letter render attempt cannot become verified")
        attempt.status = "verified"
        attempt.stage = "verified"
        attempt.verification = dict(verification)
        attempt.error = None
        attempt.completed_at = datetime.now(UTC)
        source_kind: RenderSourceKind = (
            "production" if attempt.production_id is not None else "short_episode"
        )
        source_id = attempt.production_id or attempt.short_episode_id
        if source_id is not None:
            session.add(
                _event(
                    source_kind=source_kind,
                    source_id=source_id,
                    event_type=f"{source_kind}.render_attempt_verified",
                    payload={
                        "render_attempt_id": str(attempt.id),
                        "attempt_number": attempt.attempt_number,
                        "failure_count": attempt.failure_count,
                        "verification": attempt.verification,
                    },
                )
            )


def dead_letter_latest_render_attempt(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
    *,
    error: str,
) -> RenderAttempt | None:
    with session_scope() as session:
        attempt = _latest_attempt(session, source_kind, source_id)
        if attempt is None or attempt.status == "verified":
            return None
        attempt.status = "dead_letter"
        attempt.stage = "retry_exhausted"
        workflow_error = error.strip()
        prior_error = (attempt.error or "").strip()
        if prior_error and workflow_error in {"Activity task failed", "Workflow execution failed"}:
            attempt.error = prior_error[:8000]
        elif prior_error and workflow_error and prior_error != workflow_error:
            attempt.error = (
                f"{prior_error}\nWorkflow failure: {workflow_error}"
            )[:8000]
        else:
            attempt.error = (workflow_error or prior_error)[:8000]
        attempt.completed_at = datetime.now(UTC)
        session.add(
            _event(
                source_kind=source_kind,
                source_id=source_id,
                event_type=f"{source_kind}.render_dead_lettered",
                payload={
                    "render_attempt_id": str(attempt.id),
                    "attempt_number": attempt.attempt_number,
                    "failure_count": attempt.failure_count,
                    "failure_class": attempt.last_failure_class,
                    "error": attempt.error[:1000],
                },
            )
        )
        session.flush()
        session.refresh(attempt)
        session.expunge(attempt)
        return attempt


def render_attempts_for_source(
    source_kind: RenderSourceKind,
    source_id: uuid.UUID,
) -> list[RenderAttempt]:
    with session_scope() as session:
        _source_details(session, source_kind, source_id)
        attempts = list(
            session.scalars(
                select(RenderAttempt)
                .where(_source_clause(source_kind, source_id))
                .order_by(RenderAttempt.attempt_number)
            )
        )
        for attempt in attempts:
            session.expunge(attempt)
        return attempts
