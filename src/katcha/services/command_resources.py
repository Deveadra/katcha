from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import desc, select

from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, ClipFeature, SourceItem
from katcha.production_models import Production
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.services.clip_lifecycle import clip_ids_for_channel
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem
from katcha.trend_models import TrendEvidencePacket, TrendOpportunity, TrendTopic

SUPPORTED_RESOURCE_KINDS = {
    "clip",
    "production",
    "short_episode",
    "publication",
    "trend_opportunity",
}


def _decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _clip_evidence(
    session,
    channel_profile_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> dict[str, object]:
    if resource_id not in clip_ids_for_channel(session, channel_profile_id):
        raise ValueError("clip is not available to this channel")
    clip = session.get(Clip, resource_id)
    if clip is None:
        raise ValueError(f"clip not found: {resource_id}")
    source = session.scalar(
        select(SourceItem)
        .where(SourceItem.clip_id == resource_id)
        .order_by(SourceItem.discovered_at.asc())
        .limit(1)
    )
    features = session.get(ClipFeature, resource_id)
    return {
        "kind": "clip",
        "id": str(clip.id),
        "status": clip.status,
        "title": source.title if source else None,
        "creator": source.creator if source else None,
        "platform": source.platform if source else None,
        "candidate_score": (
            _decimal(features.candidate_score) if features is not None else None
        ),
        "score_breakdown": (
            dict(features.score_breakdown or {}) if features is not None else {}
        ),
        "duration_seconds": _decimal(clip.duration_seconds),
        "created_at": clip.created_at.isoformat(),
        "context_source": "typed_resource",
    }


def _production_evidence(
    session,
    channel_profile_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> dict[str, object]:
    row = session.get(Production, resource_id)
    if row is None:
        raise ValueError(f"production not found: {resource_id}")
    if row.channel_profile_id != channel_profile_id:
        raise ValueError("production belongs to a different channel")
    return {
        "kind": "production",
        "id": str(row.id),
        "clip_id": str(row.clip_id),
        "status": row.status,
        "stage": row.stage,
        "generation": row.generation,
        "workflow_id": row.workflow_id,
        "error": row.error,
        "edit_blueprint_key": row.edit_blueprint_key,
        "edit_blueprint_version": row.edit_blueprint_version,
        "estimated_cost_usd": float(row.estimated_cost_usd or 0),
        "updated_at": row.updated_at.isoformat(),
        "context_source": "typed_resource",
    }


def _episode_evidence(
    session,
    channel_profile_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> dict[str, object]:
    row = session.get(ShortEpisode, resource_id)
    if row is None:
        raise ValueError(f"short episode not found: {resource_id}")
    if row.channel_profile_id != channel_profile_id:
        raise ValueError("short episode belongs to a different channel")
    clip_ids = list(
        session.scalars(
            select(ShortEpisodeItem.clip_id)
            .where(ShortEpisodeItem.short_episode_id == row.id)
            .order_by(ShortEpisodeItem.position)
        )
    )
    return {
        "kind": "short_episode",
        "id": str(row.id),
        "premise": row.premise,
        "status": row.status,
        "stage": row.stage,
        "generation": row.generation,
        "workflow_id": row.workflow_id,
        "item_count": row.item_count,
        "clip_ids": [str(value) for value in clip_ids],
        "format_key": row.format_key,
        "edit_blueprint_key": row.edit_blueprint_key,
        "edit_blueprint_version": row.edit_blueprint_version,
        "error": row.error,
        "estimated_cost_usd": float(row.estimated_cost_usd or 0),
        "updated_at": row.updated_at.isoformat(),
        "context_source": "typed_resource",
    }


def _publication_evidence(
    session,
    channel_profile_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> dict[str, object]:
    row = session.get(Publication, resource_id)
    if row is None:
        raise ValueError(f"publication not found: {resource_id}")
    profile = session.get(ChannelProfile, channel_profile_id)
    if profile is None or row.youtube_connection_id != profile.youtube_connection_id:
        raise ValueError("publication belongs to a different channel")
    snapshot = session.scalar(
        select(PublicationAnalyticsSnapshot)
        .where(PublicationAnalyticsSnapshot.publication_id == row.id)
        .order_by(desc(PublicationAnalyticsSnapshot.sampled_at))
        .limit(1)
    )
    analytics: dict[str, object] = {}
    if snapshot is not None:
        analytics = {
            "sampled_at": snapshot.sampled_at.isoformat(),
            "views": snapshot.views,
            "engaged_views": snapshot.engaged_views,
            "average_view_duration": _decimal(snapshot.average_view_duration),
            "average_view_percentage": _decimal(snapshot.average_view_percentage),
            "likes": snapshot.likes,
            "comments": snapshot.comments,
            "shares": snapshot.shares,
            "subscribers_gained": snapshot.subscribers_gained,
            "estimated_revenue": _decimal(snapshot.estimated_revenue),
        }
    return {
        "kind": "publication",
        "id": str(row.id),
        "title": row.title,
        "status": row.status,
        "stage": row.stage,
        "privacy_status": row.privacy_status,
        "youtube_video_id": row.youtube_video_id,
        "production_id": str(row.production_id) if row.production_id else None,
        "short_episode_id": (
            str(row.short_episode_id) if row.short_episode_id else None
        ),
        "compilation_id": str(row.compilation_id) if row.compilation_id else None,
        "error": row.error,
        "failure_reason": row.failure_reason,
        "rejection_reason": row.rejection_reason,
        "analytics": analytics,
        "updated_at": row.updated_at.isoformat(),
        "context_source": "typed_resource",
    }


def _trend_evidence(
    session,
    channel_profile_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> dict[str, object]:
    row = session.get(TrendOpportunity, resource_id)
    if row is None:
        raise ValueError(f"trend opportunity not found: {resource_id}")
    if row.channel_profile_id != channel_profile_id:
        raise ValueError("trend opportunity belongs to a different channel")
    topic = session.get(TrendTopic, row.trend_topic_id)
    packet = session.scalar(
        select(TrendEvidencePacket)
        .where(TrendEvidencePacket.trend_opportunity_id == row.id)
        .order_by(desc(TrendEvidencePacket.version))
        .limit(1)
    )
    return {
        "kind": "trend_opportunity",
        "id": str(row.id),
        "topic_id": str(row.trend_topic_id),
        "topic": topic.display_name if topic else None,
        "lifecycle": row.lifecycle,
        "rank": row.rank,
        "opportunity_score": _decimal(row.opportunity_score),
        "calibrated_score": _decimal(row.calibrated_score),
        "confidence": _decimal(row.confidence),
        "reasons": list(row.reasons or []),
        "evidence_summary": dict(row.evidence_summary or {}),
        "thesis": packet.thesis if packet else None,
        "why_now": list(packet.why_now or []) if packet else [],
        "supporting_source_count": len(packet.sources or []) if packet else 0,
        "expires_at": row.expires_at.isoformat(),
        "context_source": "typed_resource",
    }


def resolve_command_resources(
    channel_profile_id: uuid.UUID,
    refs: list[tuple[str, uuid.UUID]],
) -> list[dict[str, object]]:
    if len(refs) > 8:
        raise ValueError("at most 8 typed resources may be attached to one command")
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    with session_scope() as session:
        for kind, resource_id in refs:
            key = (kind, resource_id)
            if key in seen:
                continue
            seen.add(key)
            if kind not in SUPPORTED_RESOURCE_KINDS:
                raise ValueError(f"unsupported command resource kind: {kind}")
            if kind == "clip":
                item = _clip_evidence(session, channel_profile_id, resource_id)
            elif kind == "production":
                item = _production_evidence(session, channel_profile_id, resource_id)
            elif kind == "short_episode":
                item = _episode_evidence(session, channel_profile_id, resource_id)
            elif kind == "publication":
                item = _publication_evidence(session, channel_profile_id, resource_id)
            else:
                item = _trend_evidence(session, channel_profile_id, resource_id)
            evidence.append(item)
    return evidence


def resource_context_summary(
    evidence: list[dict[str, object]],
) -> str:
    if not evidence:
        return "No typed Katcha resource context was attached."
    labels: list[str] = []
    for item in evidence:
        kind = str(item.get("kind") or "resource").replace("_", " ")
        title = (
            item.get("title")
            or item.get("premise")
            or item.get("topic")
            or item.get("id")
        )
        status = item.get("status") or item.get("lifecycle")
        label = f"{kind}: {title}"
        if status:
            label += f" ({status})"
        labels.append(label)
    return (
        "I loaded the attached Katcha resource context directly from stored "
        "channel-scoped records: " + "; ".join(labels) + "."
    )
