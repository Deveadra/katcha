from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import desc, func, or_, select

from katcha.acquisition_models import IntelligenceRecord
from katcha.db import session_scope
from katcha.editorial_models import (
    EditorialProject,
    EditorialRevision,
    EditorialRun,
    EditorialStoryboardRevision,
)
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
    "intelligence_record",
    "editorial_project",
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


def _editorial_project_evidence(
    session,
    channel_profile_id: uuid.UUID,
    resource_id: uuid.UUID,
    *,
    selector: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, object]:
    row = session.get(EditorialProject, resource_id)
    if row is None:
        raise ValueError(f"editorial project not found: {resource_id}")
    if row.channel_profile_id != channel_profile_id:
        raise ValueError("editorial project belongs to a different channel")

    brief = dict(row.brief or {})
    source_urls = [str(value) for value in brief.get("source_urls") or []][:20]
    bindings = {
        str(key): str(value)
        for key, value in dict(brief.get("source_clip_bindings") or {}).items()
    }
    script_seed = dict(brief.get("script_seed") or {})
    latest_revision = session.scalar(
        select(EditorialRevision)
        .where(EditorialRevision.project_id == row.id)
        .order_by(desc(EditorialRevision.revision))
        .limit(1)
    )
    latest_run = session.scalar(
        select(EditorialRun)
        .where(EditorialRun.project_id == row.id)
        .order_by(desc(EditorialRun.updated_at), desc(EditorialRun.created_at))
        .limit(1)
    )
    latest_storyboard = (
        session.scalar(
            select(EditorialStoryboardRevision)
            .where(
                EditorialStoryboardRevision.project_id == row.id,
                EditorialStoryboardRevision.script_revision
                == latest_revision.revision,
            )
            .order_by(EditorialStoryboardRevision.version.desc())
            .limit(1)
        )
        if latest_revision is not None
        else None
    )

    if (selector is None) != (expected_revision is None):
        raise ValueError("editorial beat selectors require an exact revision")

    revision_summary: dict[str, object] | None = None
    selected_beat: dict[str, object] | None = None
    if latest_revision is not None:
        if (
            expected_revision is not None
            and latest_revision.revision != expected_revision
        ):
            raise ValueError(
                "editorial beat selector is stale: "
                f"expected revision {expected_revision}, "
                f"current revision {latest_revision.revision}"
            )
        draft = dict(latest_revision.draft or {})
        script = list(draft.get("script") or [])
        narration = [
            str(beat.get("narration") or "")
            for beat in script
            if isinstance(beat, dict)
        ]
        revision_summary = {
            "revision": latest_revision.revision,
            "digest": latest_revision.digest,
            "script_beat_count": len(script),
            "script_word_count": sum(
                len(text.split()) for text in narration if text.strip()
            ),
            "created_at": latest_revision.created_at.isoformat(),
        }
        if selector:
            beat = next(
                (
                    value
                    for value in script
                    if isinstance(value, dict)
                    and str(value.get("id") or value.get("beat_id") or "") == selector
                ),
                None,
            )
            if beat is None:
                raise ValueError(
                    f"editorial beat not found in latest revision: {selector}"
                )
            claim_ids = [
                str(value)
                for value in list(beat.get("claim_ids") or [])[:30]
                if value
            ]
            claims = [
                value
                for value in list(draft.get("claims") or [])
                if isinstance(value, dict)
                and str(value.get("id") or "") in set(claim_ids)
            ][:20]
            source_ids = {
                str(value)
                for claim in claims
                for value in list(claim.get("source_ids") or [])
                if value
            }
            observation_ids = {
                str(value)
                for claim in claims
                for value in list(claim.get("observation_ids") or [])
                if value
            }
            sources = [
                value
                for value in list(draft.get("sources") or [])
                if isinstance(value, dict)
                and str(value.get("id") or "") in source_ids
            ][:20]
            observations = [
                value
                for value in list(draft.get("observations") or [])
                if isinstance(value, dict)
                and str(value.get("id") or "") in observation_ids
            ][:20]
            storyboard_beat = None
            if latest_storyboard is not None:
                storyboard_beat = next(
                    (
                        value
                        for value in list(
                            dict(latest_storyboard.workspace or {}).get("beats")
                            or []
                        )
                        if isinstance(value, dict)
                        and str(value.get("beat_id") or "") == selector
                    ),
                    None,
                )
            selected_beat = {
                "id": selector,
                "revision": latest_revision.revision,
                "role": beat.get("role"),
                "narration": beat.get("narration"),
                "visual_intent": beat.get("visual_intent"),
                "planned_duration_seconds": beat.get("planned_duration_seconds"),
                "nonfactual": bool(beat.get("nonfactual")),
                "uncertainty_disclosure": beat.get("uncertainty_disclosure"),
                "claim_ids": claim_ids,
                "claims": [
                    {
                        "id": claim.get("id"),
                        "text": claim.get("text"),
                        "classification": claim.get("classification"),
                        "verification": claim.get("verification"),
                        "verification_note": claim.get("verification_note"),
                        "source_ids": list(claim.get("source_ids") or [])[:20],
                        "observation_ids": list(claim.get("observation_ids") or [])[:20],
                    }
                    for claim in claims
                ],
                "sources": [
                    {
                        "id": source.get("id"),
                        "title": source.get("title"),
                        "url": source.get("url"),
                        "category": source.get("category"),
                        "locator": source.get("locator"),
                    }
                    for source in sources
                ],
                "observations": [
                    {
                        "id": observation.get("id"),
                        "source_url": observation.get("source_url"),
                        "start_seconds": observation.get("start_seconds"),
                        "end_seconds": observation.get("end_seconds"),
                        "observation": observation.get("observation"),
                        "coverage": observation.get("coverage"),
                    }
                    for observation in observations
                ],
                "storyboard": (
                    {
                        "workspace_version": latest_storyboard.version,
                        "origin": latest_storyboard.origin,
                        "presentation_mode": dict(
                            latest_storyboard.workspace or {}
                        ).get("presentation_mode"),
                        "asset_run_id": dict(
                            latest_storyboard.workspace or {}
                        ).get("asset_run_id"),
                        "narration_id": dict(
                            dict(latest_storyboard.workspace or {}).get(
                                "narration_ids"
                            )
                            or {}
                        ).get(selector),
                        "visual": {
                            "layout": storyboard_beat.get("layout"),
                            "media": list(storyboard_beat.get("media") or [])[:2],
                            "quote_source_id": storyboard_beat.get(
                                "quote_source_id"
                            ),
                            "image_id": storyboard_beat.get("image_id"),
                            "image_ids": list(
                                storyboard_beat.get("image_ids") or []
                            )[:2],
                            "image_push_in": storyboard_beat.get(
                                "image_push_in"
                            ),
                            "overlays": list(
                                storyboard_beat.get("overlays") or []
                            )[:8],
                            "caption_position": storyboard_beat.get(
                                "caption_position", "bottom"
                            ),
                            "caption_scale": storyboard_beat.get(
                                "caption_scale", 1
                            ),
                            "caption_background": bool(
                                storyboard_beat.get("caption_background")
                            ),
                            "transition": storyboard_beat.get(
                                "transition", "cut"
                            ),
                            "transition_frames": storyboard_beat.get(
                                "transition_frames", 8
                            ),
                        },
                    }
                    if latest_storyboard is not None
                    and storyboard_beat is not None
                    else None
                ),
            }
    elif selector:
        raise ValueError("editorial project has no saved revision to select a beat from")

    storyboard_summary: dict[str, object] | None = None
    if latest_storyboard is not None:
        workspace = dict(latest_storyboard.workspace or {})
        storyboard_summary = {
            "script_revision": latest_storyboard.script_revision,
            "version": latest_storyboard.version,
            "parent_version": latest_storyboard.parent_version,
            "origin": latest_storyboard.origin,
            "presentation_mode": workspace.get("presentation_mode"),
            "asset_run_id": workspace.get("asset_run_id"),
            "assigned_beat_count": sum(
                1
                for beat in list(workspace.get("beats") or [])
                if isinstance(beat, dict)
                and beat.get("layout") != "unassigned"
            ),
            "beat_count": len(list(workspace.get("beats") or [])),
            "created_at": latest_storyboard.created_at.isoformat(),
        }

    run_summary: dict[str, object] | None = None
    if latest_run is not None:
        run_summary = {
            "id": str(latest_run.id),
            "status": latest_run.status,
            "stage": latest_run.stage,
            "target": dict(latest_run.options or {}).get("target"),
            "input_revision": latest_run.input_revision,
            "attempt": latest_run.attempt,
            "error": latest_run.error,
            "updated_at": latest_run.updated_at.isoformat(),
        }

    return {
        "kind": "editorial_project",
        "id": str(row.id),
        "title": str(brief.get("prompt") or "Editorial project"),
        "revision": row.revision,
        "target_seconds": brief.get("target_seconds"),
        "source_urls": source_urls,
        "source_count": len(source_urls),
        "managed_clip_count": len(bindings),
        "managed_clip_ids": list(dict.fromkeys(bindings.values()))[:20],
        "script_seed": (
            {
                "origin": script_seed.get("origin"),
                "filename": script_seed.get("filename"),
                "media_type": script_seed.get("media_type"),
                "content_sha256": script_seed.get("content_sha256"),
            }
            if script_seed
            else None
        ),
        "latest_revision": revision_summary,
        "latest_storyboard": storyboard_summary,
        "selected_beat": selected_beat,
        "latest_run": run_summary,
        "updated_at": row.updated_at.isoformat(),
        "context_source": "typed_resource",
    }


def _intelligence_record_evidence(
    session,
    channel_profile_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> dict[str, object]:
    row = session.get(IntelligenceRecord, resource_id)
    if row is None:
        raise ValueError(f"intelligence record not found: {resource_id}")
    if row.channel_profile_id != channel_profile_id:
        raise ValueError("intelligence record belongs to a different channel")
    return {
        "kind": "intelligence_record",
        "id": str(row.id),
        "record_kind": row.record_kind,
        "record_key": row.record_key,
        "title": row.title,
        "summary": row.summary,
        "source_url": row.source_url,
        "platform": row.platform,
        "status": row.status,
        "tags": list(row.tags or []),
        "payload": dict(row.payload or {}),
        "provenance": dict(row.provenance or {}),
        "observed_at": row.observed_at.isoformat(),
        "event_time": row.event_time.isoformat() if row.event_time else None,
        "updated_at": row.updated_at.isoformat(),
        "context_source": "typed_resource",
    }


def research_context(
    channel_profile_id: uuid.UUID,
    terms: list[str],
    *,
    offset: int = 0,
) -> tuple[str, list[dict[str, object]]]:
    """Connect existing retained research/trends to planning without manual attachments."""
    terms = list(dict.fromkeys(term.strip().casefold()[:120] for term in terms if term.strip()))[:8]
    with session_scope() as session:
        record_query = select(IntelligenceRecord).where(
            IntelligenceRecord.channel_profile_id == channel_profile_id,
            IntelligenceRecord.status == "active",
        )
        trend_query = select(TrendOpportunity).join(
            TrendTopic, TrendTopic.id == TrendOpportunity.trend_topic_id,
        ).where(
            TrendOpportunity.channel_profile_id == channel_profile_id,
            TrendOpportunity.expires_at > datetime.now(UTC),
        )
        if terms:
            record_query = record_query.where(or_(*[
                func.lower(column).contains(term, autoescape=True)
                for term in terms
                for column in (IntelligenceRecord.title, IntelligenceRecord.summary)
            ]))
            trend_query = trend_query.where(or_(*[
                func.lower(TrendTopic.display_name).contains(term, autoescape=True)
                for term in terms
            ]))
        records = list(session.scalars(record_query.order_by(
            IntelligenceRecord.observed_at.desc(), IntelligenceRecord.id,
        ).limit(4).offset(offset)))
        trends = list(session.scalars(trend_query.order_by(
            TrendOpportunity.created_at.desc(), TrendOpportunity.opportunity_score.desc(),
            TrendOpportunity.id,
        ).limit(4).offset(offset)))
        evidence = [
            _intelligence_record_evidence(session, channel_profile_id, row.id)
            for row in records
        ] + [_trend_evidence(session, channel_profile_id, row.id) for row in trends]
    return (
        f"Found {len(records)} retained research records and {len(trends)} current trend "
        "opportunities in this channel. This is a bounded saved-data lookup, not a live search "
        "or proof that media has been downloaded. No collection or production was started.",
        evidence,
    )


def resolve_command_resources(
    channel_profile_id: uuid.UUID,
    refs: list[
        tuple[str, uuid.UUID]
        | tuple[str, uuid.UUID, str | None, int | None]
    ],
) -> list[dict[str, object]]:
    if len(refs) > 8:
        raise ValueError("at most 8 typed resources may be attached to one command")
    evidence: list[dict[str, object]] = []
    seen: set[tuple[str, uuid.UUID, str | None, int | None]] = set()
    with session_scope() as session:
        for ref in refs:
            kind, resource_id, *selector_values = ref
            if len(selector_values) not in {0, 2}:
                raise ValueError(
                    "resource selectors require both a selector and exact revision"
                )
            selector = selector_values[0] if selector_values else None
            expected_revision = selector_values[1] if selector_values else None
            key = (kind, resource_id, selector, expected_revision)
            if key in seen:
                continue
            seen.add(key)
            if kind not in SUPPORTED_RESOURCE_KINDS:
                raise ValueError(f"unsupported command resource kind: {kind}")
            if selector and kind != "editorial_project":
                raise ValueError(
                    "resource selectors are only supported for editorial projects"
                )
            if (selector is None) != (expected_revision is None):
                raise ValueError("editorial beat selectors require an exact revision")
            if kind == "clip":
                item = _clip_evidence(session, channel_profile_id, resource_id)
            elif kind == "production":
                item = _production_evidence(session, channel_profile_id, resource_id)
            elif kind == "short_episode":
                item = _episode_evidence(session, channel_profile_id, resource_id)
            elif kind == "publication":
                item = _publication_evidence(session, channel_profile_id, resource_id)
            elif kind == "trend_opportunity":
                item = _trend_evidence(session, channel_profile_id, resource_id)
            elif kind == "editorial_project":
                item = _editorial_project_evidence(
                    session,
                    channel_profile_id,
                    resource_id,
                    selector=selector,
                    expected_revision=expected_revision,
                )
            else:
                item = _intelligence_record_evidence(
                    session,
                    channel_profile_id,
                    resource_id,
                )
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
