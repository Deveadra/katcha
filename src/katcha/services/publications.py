from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import (
    CompilationStatus,
    ProductionStatus,
    PublicationStatus,
    YouTubeConnectionStatus,
)
from katcha.longform_models import Compilation, CompilationAsset
from katcha.models import DomainEvent
from katcha.production_models import Production, ProductionAsset
from katcha.publishing_models import Publication, YouTubeConnection

VALID_PRIVACY_STATUSES = {"private", "unlisted", "public"}
SourceKind = Literal["production", "compilation"]


def _clean_tags(tags: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        value = raw.strip()
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        result.append(value)

    tag_budget = sum(len(tag) + (2 if " " in tag else 0) for tag in result)
    tag_budget += max(0, len(result) - 1)
    if tag_budget > 500:
        raise ValueError("YouTube tags exceed the 500-character aggregate limit")
    return result


def _validate_text_metadata(title: str, description: str) -> None:
    if not title:
        raise ValueError("publication title cannot be empty")
    if len(title) > 100:
        raise ValueError("publication title cannot exceed 100 characters")
    if "<" in title or ">" in title:
        raise ValueError("publication title cannot contain '<' or '>'")
    if len(description.encode("utf-8")) > 5000:
        raise ValueError("publication description cannot exceed 5000 UTF-8 bytes")
    if "<" in description or ">" in description:
        raise ValueError("publication description cannot contain '<' or '>'")


def _normalize_publication_metadata(
    *,
    title: str,
    description: str,
    tags: list[str] | None,
    privacy_status: str,
    publish_at: datetime | None,
) -> tuple[str, str, list[str], str, datetime | None]:
    title = title.strip()
    description = description.strip()
    cleaned_tags = _clean_tags(tags or [])
    privacy_status = privacy_status.strip().lower()

    _validate_text_metadata(title, description)
    if privacy_status not in VALID_PRIVACY_STATUSES:
        raise ValueError(f"unsupported privacy status: {privacy_status}")
    if publish_at is not None:
        if publish_at.tzinfo is None:
            raise ValueError("publish_at must include a timezone")
        publish_at = publish_at.astimezone(UTC)
        if publish_at <= datetime.now(UTC):
            raise ValueError("publish_at must be in the future")
        if privacy_status != "public":
            raise ValueError("scheduled publications must use final privacy_status='public'")
    return title, description, cleaned_tags, privacy_status, publish_at


def _approved_render_key(
    session: Session,
    *,
    source_kind: SourceKind,
    source_id: uuid.UUID,
) -> str:
    if source_kind == "production":
        source = session.get(Production, source_id)
        if source is None:
            raise ValueError(f"production not found: {source_id}")
        if source.status != ProductionStatus.APPROVED.value:
            raise ValueError("production must be approved before publication")
        render = session.scalar(
            select(ProductionAsset).where(
                ProductionAsset.production_id == source_id,
                ProductionAsset.kind == "render",
                ProductionAsset.generation == 1,
            )
        )
        if render is None:
            raise ValueError("approved production has no rendered video asset")
        return render.storage_key

    source = session.get(Compilation, source_id)
    if source is None:
        raise ValueError(f"compilation not found: {source_id}")
    if source.status != CompilationStatus.APPROVED.value:
        raise ValueError("compilation must be approved before publication")
    render = session.scalar(
        select(CompilationAsset).where(
            CompilationAsset.compilation_id == source_id,
            CompilationAsset.kind == "render",
            CompilationAsset.generation == 1,
        )
    )
    if render is None:
        raise ValueError("approved compilation has no rendered video asset")
    return render.storage_key


def _existing_publication(
    session: Session,
    *,
    source_kind: SourceKind,
    source_id: uuid.UUID,
    youtube_connection_id: uuid.UUID,
) -> Publication | None:
    source_column = (
        Publication.production_id
        if source_kind == "production"
        else Publication.compilation_id
    )
    return session.scalar(
        select(Publication).where(
            source_column == source_id,
            Publication.youtube_connection_id == youtube_connection_id,
        )
    )


def _register_source_publication(
    *,
    source_kind: SourceKind,
    source_id: uuid.UUID,
    youtube_connection_id: uuid.UUID,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str | None = None,
    privacy_status: str = "private",
    publish_at: datetime | None = None,
    notify_subscribers: bool = False,
    made_for_kids: bool = False,
    contains_synthetic_media: bool = False,
) -> Publication:
    settings = get_settings()
    title, description, tags, privacy_status, publish_at = _normalize_publication_metadata(
        title=title,
        description=description,
        tags=tags,
        privacy_status=privacy_status,
        publish_at=publish_at,
    )

    with session_scope() as session:
        existing = _existing_publication(
            session,
            source_kind=source_kind,
            source_id=source_id,
            youtube_connection_id=youtube_connection_id,
        )
        if existing is not None:
            session.expunge(existing)
            return existing

        render_key = _approved_render_key(
            session,
            source_kind=source_kind,
            source_id=source_id,
        )
        connection = session.get(YouTubeConnection, youtube_connection_id)
        if connection is None:
            raise ValueError(f"YouTube connection not found: {youtube_connection_id}")
        if connection.status != YouTubeConnectionStatus.ACTIVE.value:
            raise ValueError("YouTube connection is not active")

        publication_id = uuid.uuid4()
        workflow_id = f"yt-publish-{publication_id}-a1"
        analytics_workflow_id = f"yt-analytics-{publication_id}"
        publication = Publication(
            id=publication_id,
            production_id=source_id if source_kind == "production" else None,
            compilation_id=source_id if source_kind == "compilation" else None,
            youtube_connection_id=youtube_connection_id,
            workflow_id=workflow_id,
            workflow_attempt=1,
            analytics_workflow_id=analytics_workflow_id,
            status=PublicationStatus.QUEUED.value,
            stage="queued",
            title=title,
            description=description,
            tags=tags,
            category_id=category_id or settings.youtube_default_category_id,
            privacy_status=privacy_status,
            publish_at=publish_at,
            notify_subscribers=notify_subscribers,
            made_for_kids=made_for_kids,
            contains_synthetic_media=contains_synthetic_media,
            raw_status={
                "source_kind": source_kind,
                "render_key_at_registration": render_key,
            },
        )
        session.add(publication)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication.id),
                event_type="publication.created",
                payload={
                    "publication_id": str(publication.id),
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "production_id": (
                        str(source_id) if source_kind == "production" else None
                    ),
                    "compilation_id": (
                        str(source_id) if source_kind == "compilation" else None
                    ),
                    "youtube_connection_id": str(youtube_connection_id),
                    "workflow_id": workflow_id,
                    "privacy_status": privacy_status,
                    "publish_at": publish_at.isoformat() if publish_at else None,
                },
            )
        )
        session.refresh(publication)
        session.expunge(publication)
        return publication


def register_publication(
    production_id: uuid.UUID,
    *,
    youtube_connection_id: uuid.UUID,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str | None = None,
    privacy_status: str = "private",
    publish_at: datetime | None = None,
    notify_subscribers: bool = False,
    made_for_kids: bool = False,
    contains_synthetic_media: bool = False,
) -> Publication:
    return _register_source_publication(
        source_kind="production",
        source_id=production_id,
        youtube_connection_id=youtube_connection_id,
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        privacy_status=privacy_status,
        publish_at=publish_at,
        notify_subscribers=notify_subscribers,
        made_for_kids=made_for_kids,
        contains_synthetic_media=contains_synthetic_media,
    )


def register_compilation_publication(
    compilation_id: uuid.UUID,
    *,
    youtube_connection_id: uuid.UUID,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str | None = None,
    privacy_status: str = "private",
    publish_at: datetime | None = None,
    notify_subscribers: bool = False,
    made_for_kids: bool = False,
    contains_synthetic_media: bool = False,
) -> Publication:
    return _register_source_publication(
        source_kind="compilation",
        source_id=compilation_id,
        youtube_connection_id=youtube_connection_id,
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        privacy_status=privacy_status,
        publish_at=publish_at,
        notify_subscribers=notify_subscribers,
        made_for_kids=made_for_kids,
        contains_synthetic_media=contains_synthetic_media,
    )


def retry_publication(
    publication_id: uuid.UUID,
    *,
    allow_new_upload_session: bool = False,
) -> Publication:
    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if publication.status not in {
            PublicationStatus.FAILED.value,
            PublicationStatus.PROCESSING.value,
        }:
            raise ValueError("only failed or timed-out processing publications can be retried")

        connection = session.get(YouTubeConnection, publication.youtube_connection_id)
        if connection is None or connection.status != YouTubeConnectionStatus.ACTIVE.value:
            raise ValueError("YouTube connection must be active before retrying")

        source_kind: SourceKind
        source_id: uuid.UUID
        if publication.production_id is not None and publication.compilation_id is None:
            source_kind = "production"
            source_id = publication.production_id
        elif publication.compilation_id is not None and publication.production_id is None:
            source_kind = "compilation"
            source_id = publication.compilation_id
        else:
            raise ValueError("publication source lineage is invalid")
        _approved_render_key(session, source_kind=source_kind, source_id=source_id)

        expired_without_video_id = (
            publication.stage == "upload_session_expired"
            and publication.youtube_video_id is None
        )
        if expired_without_video_id and not allow_new_upload_session:
            raise ValueError(
                "expired upload session is ambiguous; retry with "
                "allow_new_upload_session=true only after confirming that creating a new "
                "YouTube upload will not duplicate an already-created video"
            )

        previous_workflow_id = publication.workflow_id
        publication.workflow_attempt += 1
        publication.workflow_id = (
            f"yt-publish-{publication.id}-a{publication.workflow_attempt}"
        )
        if expired_without_video_id:
            publication.encrypted_upload_url = None
            publication.upload_offset = 0
            publication.upload_size = None
        publication.status = PublicationStatus.QUEUED.value
        publication.stage = "retry_queued"
        publication.error = None
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication.id),
                event_type="publication.retry_queued",
                payload={
                    "publication_id": str(publication.id),
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "workflow_attempt": publication.workflow_attempt,
                    "previous_workflow_id": previous_workflow_id,
                    "workflow_id": publication.workflow_id,
                    "new_upload_session_authorized": bool(expired_without_video_id),
                },
            )
        )
        session.flush()
        session.refresh(publication)
        session.expunge(publication)
        return publication


def analytics_refresh_workflow_id(publication_id: uuid.UUID) -> str:
    return f"yt-analytics-refresh-{publication_id}-{uuid.uuid4().hex[:16]}"
