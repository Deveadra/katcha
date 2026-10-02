from __future__ import annotations

import uuid
from datetime import UTC, datetime

from katcha.db import session_scope
from katcha.domain import SourceStatus
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent, SourceItem
from katcha.services.packaging_generation import (
    AmbiguousPackagingGeneration,
    PackagingGenerationUnavailable,
    generate_packaging_candidates,
)
from katcha.services.productions import register_source_passthrough_production
from katcha.services.publications import register_publication


def _publication_time(value: object | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        candidate = str(value).strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError("publish_at must include a timezone")
    return parsed.astimezone(UTC)


def prepare_authorized_passthrough_source(source_id: uuid.UUID) -> dict[str, object]:
    """Turn an ingested authorized trailer into visible pre-upload work."""
    with session_scope() as session:
        source = session.get(SourceItem, source_id)
        if source is None:
            raise ValueError(f"source not found: {source_id}")
        if source.clip_id is None or source.status != SourceStatus.READY.value:
            return {
                "source_id": str(source_id),
                "action": "skipped",
                "reason": "source is not ready",
            }
        metadata = dict(source.source_metadata or {})
        discovery = dict(metadata.get("discovery_metadata") or {})
        payload = dict(discovery.get("intelligence_payload") or {})
        if not (
            bool(discovery.get("operator_authorized"))
            and discovery.get("authorization_scope") == "official_trailer_repost"
            and discovery.get("official_source_verified")
            and payload.get("production_intent") == "source_passthrough"
        ):
            return {
                "source_id": str(source_id),
                "clip_id": str(source.clip_id),
                "action": "skipped",
                "reason": "source is not an authorized official-trailer passthrough",
            }
        channel_value = metadata.get("channel_profile_id")
        if not channel_value:
            raise ValueError("authorized trailer source has no channel_profile_id")
        channel_profile_id = uuid.UUID(str(channel_value))
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None or profile.status != "active":
            raise ValueError("authorized trailer source channel profile is not active")
        youtube_connection_id = profile.youtube_connection_id
        clip_id = source.clip_id
        title = (
            str(metadata.get("intelligence_title") or "").strip()
            or str(source.title or "").strip()
            or "Untitled official trailer"
        )
        summary = str(metadata.get("intelligence_summary") or "").strip()
        tags = [
            str(value).strip()
            for value in (discovery.get("intelligence_tags") or [])
            if str(value).strip()
        ][:50]
        intelligence_record_id = str(
            metadata.get("intelligence_record_id")
            or discovery.get("intelligence_record_id")
            or ""
        ).strip()
        publish_at = _publication_time(payload.get("publish_at"))
        privacy_status = str(payload.get("privacy_status") or "public").strip().lower()
        if privacy_status not in {"private", "unlisted", "public"}:
            privacy_status = "public"
        hold_for_packaging = bool(payload.get("preupload_packaging_required", True))
        candidate_count = int(payload.get("packaging_candidate_count") or 3)
        candidate_count = min(5, max(2, candidate_count))

    production = register_source_passthrough_production(
        clip_id,
        channel_profile_id=channel_profile_id,
        idempotency_key=(
            f"handoff-{intelligence_record_id}"
            if intelligence_record_id
            else f"source-{source_id}"
        ),
        actor="intelligence_handoff",
    )
    publication = register_publication(
        production.id,
        youtube_connection_id=youtube_connection_id,
        title=title,
        description=summary,
        tags=tags,
        privacy_status=privacy_status,
        publish_at=publish_at,
        notify_subscribers=bool(payload.get("notify_subscribers", False)),
        made_for_kids=bool(payload.get("made_for_kids", False)),
        contains_synthetic_media=False,
        hold_for_packaging=hold_for_packaging,
    )

    packaging_status = "not_requested"
    packaging_generation_id: str | None = None
    if hold_for_packaging:
        generation_key = (
            f"handoff-{intelligence_record_id}-seo-v1"
            if intelligence_record_id
            else f"handoff-source-{source_id}-seo-v1"
        )
        try:
            generated = generate_packaging_candidates(
                publication.id,
                generation_key=generation_key,
                candidate_count=candidate_count,
            )
            packaging_generation_id = str(generated.generation.id)
            packaging_status = generated.generation.status
        except (
            PackagingGenerationUnavailable,
            AmbiguousPackagingGeneration,
            ValueError,
        ) as exc:
            packaging_status = "needs_attention"
            with session_scope() as session:
                session.add(
                    DomainEvent(
                        aggregate_type="publication",
                        aggregate_id=str(publication.id),
                        event_type="publication.packaging_generation_failed",
                        payload={
                            "publication_id": str(publication.id),
                            "source_id": str(source_id),
                            "intelligence_record_id": intelligence_record_id or None,
                            "error": str(exc)[:4000],
                        },
                    )
                )

    return {
        "source_id": str(source_id),
        "clip_id": str(clip_id),
        "production_id": str(production.id),
        "publication_id": str(publication.id),
        "publication_stage": publication.stage,
        "publish_at": publication.publish_at.isoformat() if publication.publish_at else None,
        "packaging_status": packaging_status,
        "packaging_generation_id": packaging_generation_id,
        "action": "prepared",
    }
