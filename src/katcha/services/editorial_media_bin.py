"""Durable, channel-scoped references for the Editorial project Media Bin."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from katcha.acquisition_models import DiscoveryCandidate, RightsAssessment
from katcha.db import session_scope
from katcha.editorial.source_intelligence.candidates import reference_url
from katcha.editorial_models import EditorialProject, EditorialRevision, ProjectMediaItem
from katcha.models import Clip, SourceItem
from katcha.services.clip_lifecycle import channel_ids_for_clip
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound

_KINDS = {"video", "still", "audio", "graphic", "citation", "unknown"}
_STATUSES = {"reference_only", "review_required", "denied", "acquired", "ready"}
_RIGHTS_STATES = {
    "not_assessed",
    "reference_only",
    "review_permitted",
    "production_cleared",
    "restricted",
    "denied",
    "revoked",
    "expired",
}


def _digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _scoped_project(
    session: Session, channel_id: uuid.UUID, project_id: uuid.UUID, revision: int
) -> None:
    project = session.get(EditorialProject, project_id)
    if project is None or project.channel_profile_id != channel_id:
        raise EditorialNotFound("Editorial project not found in this channel")
    if session.get(EditorialRevision, (project_id, revision)) is None:
        raise EditorialNotFound("Editorial project revision not found in this channel")


def add_project_media_item(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    revision: int,
    *,
    request_key: str,
    actor: str,
    kind: str,
    clip_id: uuid.UUID | None = None,
    external_reference_url: str | None = None,
    requirement_ids: list[str] | None = None,
    searchable_tags: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    source_metadata: dict[str, Any] | None = None,
    source_in_seconds: float | None = None,
    source_out_seconds: float | None = None,
    notes: str = "",
    status: str | None = None,
    rights_status: str | None = None,
    rights_assessment_id: uuid.UUID | None = None,
    rights_assessment_version: int | None = None,
) -> ProjectMediaItem:
    """Create or replay a project reference without copying canonical media bytes."""
    if not request_key.strip():
        raise ValueError("A stable request key is required")
    if kind not in _KINDS:
        raise ValueError("Unsupported project media kind")
    if (clip_id is None) == (external_reference_url is None):
        raise ValueError("Choose exactly one existing clip or external reference URL")
    if external_reference_url is not None:
        external_reference_url = reference_url(external_reference_url)
    if source_in_seconds is not None and source_in_seconds < 0:
        raise ValueError("Source in time cannot be negative")
    if source_out_seconds is not None and source_out_seconds < 0:
        raise ValueError("Source out time cannot be negative")
    if (
        source_in_seconds is not None
        and source_out_seconds is not None
        and source_out_seconds <= source_in_seconds
    ):
        raise ValueError("Source out time must follow source in time")

    default_status = "acquired" if clip_id is not None else "reference_only"
    default_rights = "not_assessed" if clip_id is not None else "reference_only"
    selected_status = status or default_status
    selected_rights = rights_status or default_rights
    if selected_status not in _STATUSES or selected_rights not in _RIGHTS_STATES:
        raise ValueError("Unsupported media or rights status")
    if selected_status == "ready" and (
        clip_id is None or selected_rights != "production_cleared"
    ):
        raise ValueError("Only a production-cleared canonical clip can be marked ready")
    if (rights_assessment_id is None) != (rights_assessment_version is None):
        raise ValueError("Rights assessment ID and version must be supplied together")

    request_id = uuid.uuid5(project_id, f"project-media-item:{request_key}")
    payload = {
        "project_id": str(project_id),
        "revision": revision,
        "channel_id": str(channel_id),
        "clip_id": str(clip_id) if clip_id else None,
        "external_reference_url": external_reference_url,
        "kind": kind,
        "status": selected_status,
        "rights_status": selected_rights,
        "rights_assessment_id": str(rights_assessment_id) if rights_assessment_id else None,
        "rights_assessment_version": rights_assessment_version,
        "requirement_ids": requirement_ids or [],
        "searchable_tags": searchable_tags or [],
        "provenance": provenance or {},
        "source_metadata": source_metadata or {},
        "source_in_seconds": source_in_seconds,
        "source_out_seconds": source_out_seconds,
        "notes": notes,
    }
    request_digest = _digest(payload)
    try:
        with session_scope() as session:
            _scoped_project(session, channel_id, project_id, revision)
            if clip_id is not None:
                if session.get(Clip, clip_id) is None:
                    raise EditorialNotFound("Selected source clip not found")
                if channel_id not in channel_ids_for_clip(session, clip_id):
                    raise EditorialConflict("Selected source clip is not available to this channel")
            assessment = None
            if rights_assessment_id is not None:
                assessment = session.get(RightsAssessment, rights_assessment_id)
                if assessment is None or assessment.version != rights_assessment_version:
                    raise EditorialNotFound("Rights assessment version not found")
                candidate = session.get(DiscoveryCandidate, assessment.discovery_candidate_id)
                candidate_channel_id = (candidate.candidate_metadata or {}).get(
                    "channel_profile_id"
                ) if candidate is not None else None
                if str(candidate_channel_id) != str(channel_id):
                    raise EditorialConflict("Rights assessment is not available to this channel")
                source = (
                    session.get(SourceItem, candidate.source_item_id)
                    if candidate is not None and candidate.source_item_id is not None
                    else None
                )
                source_matches_item = (
                    source is not None
                    and (
                        (clip_id is not None and source.clip_id == clip_id)
                        or (
                            external_reference_url is not None
                            and external_reference_url
                            in {source.source_url, source.canonical_url}
                        )
                    )
                )
                if not source_matches_item:
                    raise EditorialConflict("Rights assessment does not match this media source")
            if selected_rights == "production_cleared" and (
                assessment is None or not assessment.production_eligible
            ):
                raise EditorialConflict(
                    "Production clearance requires the matching eligible rights assessment"
                )
            previous = session.scalar(
                select(ProjectMediaItem).where(ProjectMediaItem.request_id == request_id)
            )
            if previous is not None:
                if previous.request_digest != request_digest:
                    raise EditorialConflict(
                        "Media Bin request identity was used for different data"
                    )
                session.expunge(previous)
                return previous
            row = ProjectMediaItem(
                id=request_id,
                project_id=project_id,
                revision=revision,
                channel_profile_id=channel_id,
                clip_id=clip_id,
                external_reference_url=external_reference_url,
                request_id=request_id,
                request_digest=request_digest,
                kind=kind,
                status=selected_status,
                rights_status=selected_rights,
                rights_assessment_id=rights_assessment_id,
                rights_assessment_version=rights_assessment_version,
                requirement_ids=list(requirement_ids or []),
                searchable_tags=list(searchable_tags or []),
                provenance=dict(provenance or {}),
                source_metadata=dict(source_metadata or {}),
                source_in_seconds=source_in_seconds,
                source_out_seconds=source_out_seconds,
                notes=notes,
                actor=actor,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            session.expunge(row)
            return row
    except IntegrityError as exc:
        # A concurrent identical retry can win the unique request identity.
        with session_scope() as session:
            previous = session.scalar(
                select(ProjectMediaItem).where(ProjectMediaItem.request_id == request_id)
            )
            if previous is None:
                raise exc from None
            if previous.request_digest != request_digest:
                raise EditorialConflict(
                    "Media Bin request identity was used for different data"
                ) from None
            session.expunge(previous)
            return previous


def list_project_media_items(
    channel_id: uuid.UUID, project_id: uuid.UUID, *, revision: int | None = None
) -> list[ProjectMediaItem]:
    with session_scope() as session:
        project = session.get(EditorialProject, project_id)
        if project is None or project.channel_profile_id != channel_id:
            raise EditorialNotFound("Editorial project not found in this channel")
        if revision is not None and session.get(EditorialRevision, (project_id, revision)) is None:
            raise EditorialNotFound("Editorial project revision not found in this channel")
        statement = select(ProjectMediaItem).where(
            ProjectMediaItem.project_id == project_id,
            ProjectMediaItem.channel_profile_id == channel_id,
        )
        if revision is not None:
            statement = statement.where(ProjectMediaItem.revision == revision)
        rows = list(
            session.scalars(
                statement.order_by(ProjectMediaItem.created_at, ProjectMediaItem.id)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def get_project_media_item(
    channel_id: uuid.UUID, project_id: uuid.UUID, item_id: uuid.UUID
) -> ProjectMediaItem:
    with session_scope() as session:
        row = session.scalar(
            select(ProjectMediaItem).where(
                ProjectMediaItem.id == item_id,
                ProjectMediaItem.project_id == project_id,
                ProjectMediaItem.channel_profile_id == channel_id,
            )
        )
        if row is None:
            raise EditorialNotFound("Project media item not found in this channel")
        session.expunge(row)
        return row
