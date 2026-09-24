from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import ChannelStatus, PublicationStatus
from katcha.intelligence_models import ChannelProfile
from katcha.packaging_models import PackagingCandidateGeneration, PublicationPackagingVariant
from katcha.publishing_models import Publication
from katcha.services.packaging_baseline import record_initial_packaging
from katcha.services.packaging_generation import (
    AmbiguousPackagingGeneration,
    generate_packaging_candidates,
)
from katcha.services.packaging_thumbnails import build_packaging_thumbnail


def seed_channel_packaging(channel_profile_id: uuid.UUID) -> dict[str, Any]:
    """Bounded, replay-safe initial packaging for published channel videos."""
    with session_scope() as session:
        channel = session.get(ChannelProfile, channel_profile_id)
        if channel is None or channel.status != ChannelStatus.ACTIVE.value:
            return {"status": "skipped", "reason": "channel_inactive"}
        publication_ids = list(
            session.scalars(
                select(Publication.id)
                .where(
                    Publication.youtube_connection_id == channel.youtube_connection_id,
                    Publication.status == PublicationStatus.PUBLISHED.value,
                    Publication.youtube_video_id.is_not(None),
                )
                .order_by(Publication.created_at.desc())
                .limit(200)
            )
        )

    settings = get_settings()
    ai_ready = bool(settings.ai_enabled and (settings.openai_api_key or settings.gemini_api_key))
    generated: list[str] = []
    rendered: list[str] = []
    blocked: list[dict[str, str]] = []
    attempted = 0
    for publication_id in publication_ids:
        try:
            record_initial_packaging(publication_id)
            with session_scope() as session:
                generation = session.scalar(
                    select(PackagingCandidateGeneration).where(
                        PackagingCandidateGeneration.publication_id == publication_id,
                        PackagingCandidateGeneration.generation_key == "auto-initial-v1",
                    )
                )
                status = generation.status if generation else None
                existing_ids = list(generation.accepted_variant_ids or []) if generation else []
                derived_parents = {
                    str((item.variant_metadata or {}).get("thumbnail_parent_variant_id"))
                    for item in session.scalars(
                        select(PublicationPackagingVariant).where(
                            PublicationPackagingVariant.publication_id == publication_id,
                        )
                    )
                    if (item.variant_metadata or {}).get("thumbnail_parent_variant_id")
                }
                pending_thumbnails = []
                for raw_id in existing_ids:
                    variant = session.get(PublicationPackagingVariant, uuid.UUID(raw_id))
                    if variant is None or variant.publication_id != publication_id:
                        continue
                    if (
                        str(variant.id) not in derived_parents
                        and variant.thumbnail_storage_key is None
                        and (variant.variant_metadata or {}).get("thumbnail_brief")
                    ):
                        pending_thumbnails.append(variant.id)
            if status in {"ambiguous", "failed"}:
                blocked.append({"publication_id": str(publication_id), "reason": status})
                continue
            if status == "completed" and not pending_thumbnails:
                continue
            if not ai_ready and status != "completed":
                blocked.append({"publication_id": str(publication_id), "reason": "ai_disabled"})
                continue
            if attempted >= 4:
                break
            attempted += 1
            if status != "completed":
                result = generate_packaging_candidates(
                    publication_id,
                    generation_key="auto-initial-v1",
                    candidate_count=3,
                )
                generated.append(str(result.generation.id))
                pending_thumbnails = [
                    variant.id
                    for variant in result.variants
                    if variant.thumbnail_storage_key is None
                    and (variant.variant_metadata or {}).get("thumbnail_brief")
                ]
            for variant_id in pending_thumbnails[:3]:
                result = build_packaging_thumbnail(publication_id, parent_variant_id=variant_id)
                rendered.append(str(result.thumbnail_variant.id))
        except AmbiguousPackagingGeneration:
            blocked.append({"publication_id": str(publication_id), "reason": "provider_ambiguous"})
        except Exception as exc:
            # Credentials, provider response bodies and creative context stay out of events.
            blocked.append({"publication_id": str(publication_id), "reason": type(exc).__name__})
    return {
        "status": "completed",
        "publications_considered": len(publication_ids),
        "generated": generated,
        "rendered": rendered,
        "blocked": blocked[:50],
    }
