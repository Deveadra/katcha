from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.packaging_models import (
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication


def record_initial_packaging(publication_id: uuid.UUID) -> uuid.UUID | None:
    """Freeze the title/description already used at upload; no YouTube mutation."""
    with session_scope() as session:
        publication = session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        if not publication.youtube_video_id or publication.status not in {
            "published",
            "scheduled",
            "private",
            "unlisted",
        }:
            return None
        existing = session.scalar(
            select(PublicationPackagingActivation)
            .where(
                PublicationPackagingActivation.publication_id == publication_id,
            )
            .order_by(PublicationPackagingActivation.created_at)
            .limit(1)
        )
        if existing is not None:
            return existing.variant_id
        variant = PublicationPackagingVariant(
            publication_id=publication_id,
            variant_key="initial-published",
            version=1,
            title=publication.title,
            description=publication.description or "",
            created_by="katcha-publication",
            variant_metadata={"source": "recorded_publication_state", "thumbnail_unverified": True},
        )
        session.add(variant)
        session.flush()
        applied_at = publication.published_at or datetime.now(UTC)
        activation = PublicationPackagingActivation(
            publication_id=publication_id,
            variant_id=variant.id,
            activation_key="initial-publication-state-v1",
            workflow_id=f"yt-packaging-initial-{publication_id}",
            status="applied",
            stage="completed",
            youtube_video_id=publication.youtube_video_id,
            applied_at=applied_at,
            activation_metadata={
                "source": "recorded_publication_state",
                "no_provider_mutation": True,
            },
            created_at=applied_at,
        )
        session.add(activation)
        session.flush()
        publication.treatment_metadata = {
            **dict(publication.treatment_metadata or {}),
            "active_packaging": {
                "activation_id": str(activation.id),
                "variant_id": str(variant.id),
                "variant_key": variant.variant_key,
                "version": variant.version,
                "title": variant.title,
                "thumbnail_storage_key": None,
                "thumbnail_sha256": None,
                "activated_at": applied_at.isoformat(),
            },
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication_id),
                event_type="publication.packaging_baseline_recorded",
                payload={
                    "publication_id": str(publication_id),
                    "variant_id": str(variant.id),
                    "activation_id": str(activation.id),
                    "no_provider_mutation": True,
                },
            )
        )
        return variant.id
