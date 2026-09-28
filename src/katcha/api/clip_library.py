from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select

from katcha.clip_lifecycle_models import ClipLifecycle, ClipRetentionPolicy
from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, ClipFeature, SourceItem
from katcha.services.clip_lifecycle import (
    archive_clip,
    channel_info_for_clip,
    clip_ids_for_channel,
    clip_reference_summary,
    ensure_lifecycle,
    get_retention_policy,
    preview_channel_maintenance,
    purge_clip_media,
    refresh_search_document,
    restore_clip,
    run_channel_maintenance,
    update_library_metadata,
    upsert_retention_policy,
)

router = APIRouter(prefix="/v1", tags=["clip-library"])


class ClipChannelRef(BaseModel):
    id: uuid.UUID
    name: str


class ClipLibraryItem(BaseModel):
    id: uuid.UUID
    sha256: str
    extension: str | None
    size_bytes: int | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    status: str
    lifecycle_state: str
    tags: list[str]
    library_metadata: dict[str, Any]
    embedding_metadata: dict[str, Any]
    archived_at: datetime | None
    purged_at: datetime | None
    created_at: datetime
    updated_at: datetime
    title: str | None
    creator: str | None
    platform: str | None
    candidate_score: float | None
    channels: list[ClipChannelRef]
    active_reference_count: int
    reference_count: int


class ClipLibraryPage(BaseModel):
    items: list[ClipLibraryItem]
    total: int
    offset: int
    limit: int


class ClipMetadataUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=50)
    topic: str | None = Field(default=None, max_length=200)
    content_type: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=4000)
    actor: str = Field(default="operator", min_length=1, max_length=128)


class ClipLifecycleAction(BaseModel):
    actor: str = Field(default="operator", min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)


class ClipPurgeAction(ClipLifecycleAction):
    confirmation_text: str


class ClipLifecycleResponse(BaseModel):
    clip_id: uuid.UUID
    lifecycle_state: str
    archive_key: str | None
    tags: list[str]
    library_metadata: dict[str, Any]
    embedding_metadata: dict[str, Any]
    archived_at: datetime | None
    purged_at: datetime | None


class RetentionPolicyResponse(BaseModel):
    channel_profile_id: uuid.UUID
    retention_mode: str
    auto_archive: bool
    auto_purge: bool
    auto_remove_duplicates: bool
    archive_after_days: int | None
    purge_after_days: int | None
    failed_purge_after_days: int | None
    confirmed_at: datetime | None
    confirmed_by: str | None


class RetentionPolicyUpdate(BaseModel):
    retention_mode: Literal["indefinite", "managed"] = "indefinite"
    auto_archive: bool = False
    auto_purge: bool = False
    auto_remove_duplicates: bool = False
    archive_after_days: int | None = Field(default=None, ge=1, le=36500)
    purge_after_days: int | None = Field(default=None, ge=1, le=36500)
    failed_purge_after_days: int | None = Field(default=None, ge=1, le=36500)
    actor: str = Field(default="operator", min_length=1, max_length=128)
    acknowledge_irreversible: bool = False
    confirmation_text: str | None = Field(default=None, max_length=300)


def _channel_name(profile: ChannelProfile) -> str:
    metadata = dict(profile.profile_metadata or {})
    return str(
        metadata.get("channel_title")
        or metadata.get("name")
        or metadata.get("channel_handle")
        or profile.id
    )


def _lifecycle_response(row: ClipLifecycle) -> ClipLifecycleResponse:
    return ClipLifecycleResponse(
        clip_id=row.clip_id,
        lifecycle_state=row.lifecycle_state,
        archive_key=row.archive_key,
        tags=list(row.tags or []),
        library_metadata=dict(row.library_metadata or {}),
        embedding_metadata=dict(row.embedding_metadata or {}),
        archived_at=row.archived_at,
        purged_at=row.purged_at,
    )


def _policy_response(
    channel_profile_id: uuid.UUID,
    row: ClipRetentionPolicy | None,
) -> RetentionPolicyResponse:
    if row is None:
        return RetentionPolicyResponse(
            channel_profile_id=channel_profile_id,
            retention_mode="indefinite",
            auto_archive=False,
            auto_purge=False,
            auto_remove_duplicates=False,
            archive_after_days=None,
            purge_after_days=None,
            failed_purge_after_days=None,
            confirmed_at=None,
            confirmed_by=None,
        )
    return RetentionPolicyResponse(
        channel_profile_id=channel_profile_id,
        retention_mode=row.retention_mode,
        auto_archive=row.auto_archive,
        auto_purge=row.auto_purge,
        auto_remove_duplicates=row.auto_remove_duplicates,
        archive_after_days=row.archive_after_days,
        purge_after_days=row.purge_after_days,
        failed_purge_after_days=row.failed_purge_after_days,
        confirmed_at=row.confirmed_at,
        confirmed_by=row.confirmed_by,
    )


@router.get("/clips/library", response_model=ClipLibraryPage)
def list_clip_library(
    q: str | None = Query(default=None, max_length=500),
    channel_profile_id: uuid.UUID | None = Query(default=None),
    clip_status: str | None = Query(default=None, alias="status", max_length=32),
    lifecycle_state: Literal["hot", "archived", "purged"] | None = Query(default=None),
    limit: int = Query(default=80, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> ClipLibraryPage:
    with session_scope() as session:
        stmt = select(Clip).outerjoin(ClipLifecycle, ClipLifecycle.clip_id == Clip.id)

        if channel_profile_id is not None:
            channel_ids = clip_ids_for_channel(session, channel_profile_id)
            if not channel_ids:
                return ClipLibraryPage(items=[], total=0, offset=offset, limit=limit)
            stmt = stmt.where(Clip.id.in_(channel_ids))

        if clip_status:
            stmt = stmt.where(Clip.status == clip_status)
        if lifecycle_state == "hot":
            stmt = stmt.where(
                or_(
                    ClipLifecycle.lifecycle_state == "hot",
                    ClipLifecycle.lifecycle_state.is_(None),
                )
            )
        elif lifecycle_state:
            stmt = stmt.where(ClipLifecycle.lifecycle_state == lifecycle_state)

        cleaned_query = (q or "").strip()
        if cleaned_query:
            pattern = f"%{cleaned_query}%"
            source_ids = select(SourceItem.clip_id).where(
                SourceItem.clip_id.is_not(None),
                or_(
                    SourceItem.title.ilike(pattern),
                    SourceItem.creator.ilike(pattern),
                    SourceItem.platform.ilike(pattern),
                    SourceItem.source_url.ilike(pattern),
                    SourceItem.canonical_url.ilike(pattern),
                ),
            )
            stmt = stmt.where(
                or_(
                    cast(Clip.id, String).ilike(pattern),
                    Clip.sha256.ilike(pattern),
                    ClipLifecycle.search_document.ilike(pattern),
                    Clip.id.in_(source_ids),
                )
            )

        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(session.scalar(count_stmt) or 0)
        clips = list(
            session.scalars(
                stmt.order_by(Clip.created_at.desc(), Clip.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        clip_ids = [clip.id for clip in clips]
        sources_by_clip: dict[uuid.UUID, list[SourceItem]] = {
            clip_id: [] for clip_id in clip_ids
        }
        if clip_ids:
            for source in session.scalars(
                select(SourceItem)
                .where(SourceItem.clip_id.in_(clip_ids))
                .order_by(SourceItem.discovered_at.asc())
            ):
                if source.clip_id is not None:
                    sources_by_clip[source.clip_id].append(source)

        items: list[ClipLibraryItem] = []
        for clip in clips:
            lifecycle = ensure_lifecycle(session, clip)
            if not lifecycle.search_document:
                refresh_search_document(session, clip, lifecycle)
            sources = sources_by_clip.get(clip.id, [])
            primary = sources[0] if sources else None
            features = session.get(ClipFeature, clip.id)
            channels = [
                ClipChannelRef(id=uuid.UUID(item["id"]), name=item["name"])
                for item in channel_info_for_clip(session, clip.id)
            ]
            refs = clip_reference_summary(session, clip.id)
            items.append(
                ClipLibraryItem(
                    id=clip.id,
                    sha256=clip.sha256,
                    extension=clip.extension,
                    size_bytes=clip.size_bytes,
                    duration_seconds=(
                        float(clip.duration_seconds)
                        if clip.duration_seconds is not None
                        else None
                    ),
                    width=clip.width,
                    height=clip.height,
                    status=clip.status,
                    lifecycle_state=lifecycle.lifecycle_state,
                    tags=list(lifecycle.tags or []),
                    library_metadata=dict(lifecycle.library_metadata or {}),
                    embedding_metadata=dict(lifecycle.embedding_metadata or {}),
                    archived_at=lifecycle.archived_at,
                    purged_at=lifecycle.purged_at,
                    created_at=clip.created_at,
                    updated_at=clip.updated_at,
                    title=primary.title if primary else None,
                    creator=primary.creator if primary else None,
                    platform=primary.platform if primary else None,
                    candidate_score=(
                        float(features.candidate_score)
                        if features and features.candidate_score is not None
                        else None
                    ),
                    channels=channels,
                    active_reference_count=refs.active_count,
                    reference_count=(
                        refs.production_count
                        + refs.short_episode_count
                        + refs.compilation_count
                    ),
                )
            )
        return ClipLibraryPage(
            items=items,
            total=total,
            offset=offset,
            limit=limit,
        )


@router.patch(
    "/clips/{clip_id}/library-metadata",
    response_model=ClipLifecycleResponse,
)
def update_clip_library_metadata(
    clip_id: uuid.UUID,
    request: ClipMetadataUpdate,
) -> ClipLifecycleResponse:
    try:
        row = update_library_metadata(
            clip_id,
            tags=request.tags,
            library_metadata={
                "topic": request.topic,
                "content_type": request.content_type,
                "notes": request.notes,
            },
            actor=request.actor,
        )
        return _lifecycle_response(row)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/clips/{clip_id}/archive",
    response_model=ClipLifecycleResponse,
)
def archive_clip_media(
    clip_id: uuid.UUID,
    request: ClipLifecycleAction,
) -> ClipLifecycleResponse:
    try:
        return _lifecycle_response(
            archive_clip(
                clip_id,
                actor=request.actor,
                reason=request.reason,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/clips/{clip_id}/restore",
    response_model=ClipLifecycleResponse,
)
def restore_clip_media(
    clip_id: uuid.UUID,
    request: ClipLifecycleAction,
) -> ClipLifecycleResponse:
    try:
        return _lifecycle_response(
            restore_clip(
                clip_id,
                actor=request.actor,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/clips/{clip_id}/purge",
    response_model=ClipLifecycleResponse,
)
def purge_clip(
    clip_id: uuid.UUID,
    request: ClipPurgeAction,
) -> ClipLifecycleResponse:
    expected = f"DELETE {clip_id}"
    if request.confirmation_text.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'type "{expected}" to confirm permanent media deletion',
        )
    try:
        return _lifecycle_response(
            purge_clip_media(
                clip_id,
                actor=request.actor,
                reason=request.reason,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/channels/{channel_profile_id}/clip-retention",
    response_model=RetentionPolicyResponse,
)
def get_channel_clip_retention(
    channel_profile_id: uuid.UUID,
) -> RetentionPolicyResponse:
    with session_scope() as session:
        if session.get(ChannelProfile, channel_profile_id) is None:
            raise HTTPException(status_code=404, detail="channel profile not found")
    return _policy_response(
        channel_profile_id,
        get_retention_policy(channel_profile_id),
    )


@router.put(
    "/channels/{channel_profile_id}/clip-retention",
    response_model=RetentionPolicyResponse,
)
def update_channel_clip_retention(
    channel_profile_id: uuid.UUID,
    request: RetentionPolicyUpdate,
) -> RetentionPolicyResponse:
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="channel profile not found")
        channel_name = _channel_name(profile)

    confirmed = False
    if request.auto_purge:
        expected = f"ENABLE AUTO DELETE {channel_name}"
        confirmed = (
            request.acknowledge_irreversible
            and request.confirmation_text == expected
        )
        if not confirmed:
            raise HTTPException(
                status_code=400,
                detail=(
                    "automatic deletion requires the irreversible-action checkbox "
                    f'and exact phrase "{expected}"'
                ),
            )
    try:
        row = upsert_retention_policy(
            channel_profile_id,
            retention_mode=request.retention_mode,
            auto_archive=request.auto_archive,
            auto_purge=request.auto_purge,
            auto_remove_duplicates=request.auto_remove_duplicates,
            archive_after_days=request.archive_after_days,
            purge_after_days=request.purge_after_days,
            failed_purge_after_days=request.failed_purge_after_days,
            actor=request.actor,
            auto_delete_confirmed=confirmed,
        )
        return _policy_response(channel_profile_id, row)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/channels/{channel_profile_id}/clip-retention/preview")
def preview_channel_clip_retention(channel_profile_id: uuid.UUID) -> dict[str, object]:
    try:
        return preview_channel_maintenance(channel_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/channels/{channel_profile_id}/clip-retention/run")
def run_channel_clip_retention(channel_profile_id: uuid.UUID) -> dict[str, object]:
    try:
        return run_channel_maintenance(
            channel_profile_id,
            actor="operator-maintenance",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
