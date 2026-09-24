from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import YouTubeConnectionStatus
from katcha.integrations.storage import ObjectStore
from katcha.models import DomainEvent
from katcha.packaging_models import (
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication, YouTubeConnection

_MAX_THUMBNAIL_BYTES = 50 * 1024 * 1024
_KEY_RE = re.compile(r"[a-z0-9_-]+")
_ALLOWED_EXTENSIONS = (".png", ".jpg", ".jpeg")


def _validate_publication_text(title: str, description: str) -> tuple[str, str]:
    cleaned_title = title.strip()
    cleaned_description = description.strip()
    if not cleaned_title:
        raise ValueError("packaging title cannot be empty")
    if len(cleaned_title) > 100:
        raise ValueError("packaging title cannot exceed 100 characters")
    if "<" in cleaned_title or ">" in cleaned_title:
        raise ValueError("packaging title cannot contain '<' or '>'")
    if len(cleaned_description.encode("utf-8")) > 5000:
        raise ValueError("packaging description cannot exceed 5000 UTF-8 bytes")
    if "<" in cleaned_description or ">" in cleaned_description:
        raise ValueError("packaging description cannot contain '<' or '>'")
    return cleaned_title, cleaned_description


def _validate_variant_identity(variant_key: str, version: int) -> str:
    key = variant_key.strip().casefold()
    if not _KEY_RE.fullmatch(key):
        raise ValueError("variant_key must contain only lowercase letters, digits, '_' or '-'")
    if version < 1:
        raise ValueError("packaging variant version must be positive")
    return key


def _thumbnail_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    raise ValueError("packaging thumbnail must contain PNG or JPEG image bytes")


def _freeze_thumbnail(
    publication_id: uuid.UUID,
    *,
    variant_key: str,
    version: int,
    storage_key: str | None,
    store: ObjectStore,
) -> dict[str, object | None]:
    if storage_key is None:
        return {
            "thumbnail_storage_key": None,
            "thumbnail_content_type": None,
            "thumbnail_size_bytes": None,
            "thumbnail_sha256": None,
        }
    key = storage_key.strip()
    expected_prefix = f"packaging/{publication_id}/{variant_key}/v{version}/"
    if not key.startswith(expected_prefix):
        raise ValueError(
            "thumbnail storage key must use the publication/version packaging namespace"
        )
    if ".." in key or "\\" in key:
        raise ValueError("thumbnail storage key contains an unsafe path segment")
    if not key.casefold().endswith(_ALLOWED_EXTENSIONS):
        raise ValueError("packaging thumbnail must use .png, .jpg or .jpeg")
    if not store.exists(key):
        raise ValueError("packaging thumbnail object does not exist")
    stat = store.stat(key)
    size_bytes = int(stat.get("size_bytes") or 0)
    if size_bytes <= 0:
        raise ValueError("packaging thumbnail object is empty")
    if size_bytes > _MAX_THUMBNAIL_BYTES:
        raise ValueError("packaging thumbnail exceeds YouTube's 50MB upload limit")
    data = store.get_bytes(key)
    if len(data) != size_bytes:
        raise ValueError("packaging thumbnail changed while it was being frozen")
    content_type = _thumbnail_type(data)
    return {
        "thumbnail_storage_key": key,
        "thumbnail_content_type": content_type,
        "thumbnail_size_bytes": size_bytes,
        "thumbnail_sha256": hashlib.sha256(data).hexdigest(),
    }


def _variant_matches(
    variant: PublicationPackagingVariant,
    *,
    title: str,
    description: str,
    frozen_thumbnail: dict[str, object | None],
    created_by: str,
    metadata: dict[str, Any],
) -> bool:
    return (
        variant.title == title
        and variant.description == description
        and variant.thumbnail_storage_key == frozen_thumbnail["thumbnail_storage_key"]
        and variant.thumbnail_content_type == frozen_thumbnail["thumbnail_content_type"]
        and variant.thumbnail_size_bytes == frozen_thumbnail["thumbnail_size_bytes"]
        and variant.thumbnail_sha256 == frozen_thumbnail["thumbnail_sha256"]
        and variant.created_by == created_by
        and dict(variant.variant_metadata or {}) == metadata
    )


def create_packaging_variant(
    publication_id: uuid.UUID,
    *,
    variant_key: str,
    version: int,
    title: str,
    description: str = "",
    thumbnail_storage_key: str | None = None,
    created_by: str = "operator",
    metadata: dict[str, Any] | None = None,
    store: ObjectStore | None = None,
) -> PublicationPackagingVariant:
    key = _validate_variant_identity(variant_key, version)
    clean_title, clean_description = _validate_publication_text(title, description)
    actor = created_by.strip()
    if not actor or len(actor) > 128:
        raise ValueError("created_by must be between 1 and 128 characters")
    payload = dict(metadata or {})

    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")

    object_store = store or ObjectStore()
    frozen = _freeze_thumbnail(
        publication_id,
        variant_key=key,
        version=version,
        storage_key=thumbnail_storage_key,
        store=object_store,
    )

    with session_scope() as session:
        existing = session.scalar(
            select(PublicationPackagingVariant).where(
                PublicationPackagingVariant.publication_id == publication_id,
                PublicationPackagingVariant.variant_key == key,
                PublicationPackagingVariant.version == version,
            )
        )
        if existing is not None:
            if not _variant_matches(
                existing,
                title=clean_title,
                description=clean_description,
                frozen_thumbnail=frozen,
                created_by=actor,
                metadata=payload,
            ):
                raise ValueError(
                    "packaging variant key/version already exists with different immutable data"
                )
            session.expunge(existing)
            return existing

        variant = PublicationPackagingVariant(
            publication_id=publication_id,
            variant_key=key,
            version=version,
            title=clean_title,
            description=clean_description,
            created_by=actor,
            variant_metadata=payload,
            **frozen,
        )
        session.add(variant)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication_id),
                event_type="publication.packaging_variant_created",
                payload={
                    "publication_id": str(publication_id),
                    "packaging_variant_id": str(variant.id),
                    "variant_key": key,
                    "version": version,
                    "has_thumbnail": variant.thumbnail_storage_key is not None,
                    "thumbnail_sha256": variant.thumbnail_sha256,
                },
            )
        )
        session.refresh(variant)
        session.expunge(variant)
        return variant


def list_packaging_variants(
    publication_id: uuid.UUID,
) -> list[PublicationPackagingVariant]:
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise ValueError(f"publication not found: {publication_id}")
        rows = list(
            session.scalars(
                select(PublicationPackagingVariant)
                .where(PublicationPackagingVariant.publication_id == publication_id)
                .order_by(
                    PublicationPackagingVariant.variant_key,
                    PublicationPackagingVariant.version,
                )
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def register_packaging_activation(
    publication_id: uuid.UUID,
    *,
    variant_id: uuid.UUID,
    activation_key: str,
) -> PublicationPackagingActivation:
    key = activation_key.strip()
    if not key:
        raise ValueError("activation_key is required")
    if len(key) > 160:
        raise ValueError("activation_key must be 160 characters or fewer")

    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if not publication.youtube_video_id:
            raise ValueError("publication must have a YouTube video ID before packaging activation")
        connection = session.get(YouTubeConnection, publication.youtube_connection_id)
        if connection is None or connection.status != YouTubeConnectionStatus.ACTIVE.value:
            raise ValueError("publication YouTube connection is not active")
        variant = session.get(PublicationPackagingVariant, variant_id)
        if variant is None or variant.publication_id != publication_id:
            raise ValueError("packaging variant does not belong to this publication")

        existing = session.scalar(
            select(PublicationPackagingActivation).where(
                PublicationPackagingActivation.publication_id == publication_id,
                PublicationPackagingActivation.activation_key == key,
            )
        )
        if existing is not None:
            if existing.variant_id != variant_id:
                raise ValueError("activation_key already belongs to a different packaging variant")
            session.expunge(existing)
            return existing

        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        activation = PublicationPackagingActivation(
            publication_id=publication_id,
            variant_id=variant_id,
            activation_key=key,
            workflow_id=f"yt-packaging-{publication_id}-{digest}",
            status="queued",
            stage="queued",
            youtube_video_id=publication.youtube_video_id,
        )
        session.add(activation)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication_id),
                event_type="publication.packaging_activation_registered",
                payload={
                    "publication_id": str(publication_id),
                    "packaging_activation_id": str(activation.id),
                    "packaging_variant_id": str(variant_id),
                    "activation_key": key,
                    "youtube_video_id": publication.youtube_video_id,
                },
            )
        )
        session.refresh(activation)
        session.expunge(activation)
        return activation


def list_packaging_activations(
    publication_id: uuid.UUID,
) -> list[PublicationPackagingActivation]:
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise ValueError(f"publication not found: {publication_id}")
        rows = list(
            session.scalars(
                select(PublicationPackagingActivation)
                .where(PublicationPackagingActivation.publication_id == publication_id)
                .order_by(PublicationPackagingActivation.created_at)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
