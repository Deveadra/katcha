from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.acquisition_models import DiscoveryCandidate
from katcha.clip_lifecycle_models import ClipLifecycle, ClipRetentionPolicy
from katcha.db import session_scope
from katcha.integrations.storage import ObjectStore
from katcha.intelligence_models import ChannelProfile
from katcha.longform_models import Compilation, CompilationSegment
from katcha.models import Clip, ClipFeature, DomainEvent, SourceItem
from katcha.production_models import Production
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem

_ACTIVE_PRODUCTION_STATUSES = {
    "queued",
    "scripting",
    "scripted",
    "voicing",
    "voiced",
    "rendering",
    "review",
}
_ACTIVE_EPISODE_STATUSES = {
    "planned",
    "queued",
    "scripting",
    "scripted",
    "voicing",
    "voiced",
    "rendering",
    "review",
}
_ACTIVE_COMPILATION_STATUSES = {
    "queued",
    "selecting",
    "planning",
    "critiquing",
    "scripted",
    "voicing",
    "voiced",
    "rendering",
    "review",
}
_DUPLICATE_SIMILARITY = 0.94


@dataclass(frozen=True, slots=True)
class ClipReferenceSummary:
    production_count: int
    short_episode_count: int
    compilation_count: int
    active_count: int

    @property
    def referenced(self) -> bool:
        return bool(
            self.production_count
            or self.short_episode_count
            or self.compilation_count
        )


def _channel_name(profile: ChannelProfile) -> str:
    metadata = dict(profile.profile_metadata or {})
    return str(
        metadata.get("channel_title")
        or metadata.get("name")
        or metadata.get("channel_handle")
        or profile.id
    )


def _metadata_channel_id(value: object) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def channel_ids_for_clip(session: Session, clip_id: uuid.UUID) -> set[uuid.UUID]:
    ids: set[uuid.UUID] = {
        value
        for value in session.scalars(
            select(Production.channel_profile_id).where(
                Production.clip_id == clip_id,
                Production.channel_profile_id.is_not(None),
            )
        )
        if value is not None
    }
    ids.update(
        session.scalars(
            select(ShortEpisode.channel_profile_id)
            .join(
                ShortEpisodeItem,
                ShortEpisodeItem.short_episode_id == ShortEpisode.id,
            )
            .where(ShortEpisodeItem.clip_id == clip_id)
        )
    )
    ids.update(
        value
        for value in session.scalars(
            select(Compilation.channel_profile_id)
            .join(
                CompilationSegment,
                CompilationSegment.compilation_id == Compilation.id,
            )
            .where(
                CompilationSegment.clip_id == clip_id,
                Compilation.channel_profile_id.is_not(None),
            )
        )
        if value is not None
    )

    from katcha.content_models import ContentItem

    ids.update(
        session.scalars(
            select(ContentItem.channel_profile_id).where(
                (ContentItem.clip_id == clip_id)
                | ContentItem.source_id.in_(
                    select(SourceItem.id).where(SourceItem.clip_id == clip_id)
                )
            )
        )
    )
    sources = list(session.scalars(select(SourceItem).where(SourceItem.clip_id == clip_id)))
    for source in sources:
        candidate_id = (source.source_metadata or {}).get("discovery_candidate_id")
        channel_id = _metadata_channel_id((source.source_metadata or {}).get("channel_profile_id"))
        if channel_id is not None:
            ids.add(channel_id)
        if candidate_id:
            try:
                candidate = session.get(DiscoveryCandidate, uuid.UUID(str(candidate_id)))
            except ValueError:
                candidate = None
            if candidate is not None:
                candidate_channel = _metadata_channel_id(
                    (candidate.candidate_metadata or {}).get("channel_profile_id")
                )
                if candidate_channel is not None:
                    ids.add(candidate_channel)
    return ids


def channel_info_for_clip(
    session: Session,
    clip_id: uuid.UUID,
) -> list[dict[str, str]]:
    channel_ids = channel_ids_for_clip(session, clip_id)
    if not channel_ids:
        return []
    profiles = list(
        session.scalars(
            select(ChannelProfile).where(ChannelProfile.id.in_(channel_ids))
        )
    )
    return [
        {"id": str(profile.id), "name": _channel_name(profile)}
        for profile in sorted(profiles, key=_channel_name)
    ]


def clip_ids_for_channel(
    session: Session,
    channel_profile_id: uuid.UUID,
) -> set[uuid.UUID]:
    ids: set[uuid.UUID] = set(
        session.scalars(
            select(Production.clip_id).where(
                Production.channel_profile_id == channel_profile_id
            )
        )
    )
    ids.update(
        session.scalars(
            select(ShortEpisodeItem.clip_id)
            .join(
                ShortEpisode,
                ShortEpisodeItem.short_episode_id == ShortEpisode.id,
            )
            .where(ShortEpisode.channel_profile_id == channel_profile_id)
        )
    )
    ids.update(
        session.scalars(
            select(CompilationSegment.clip_id)
            .join(
                Compilation,
                CompilationSegment.compilation_id == Compilation.id,
            )
            .where(Compilation.channel_profile_id == channel_profile_id)
        )
    )
    ids.update(
        value
        for value in session.scalars(
            select(SourceItem.clip_id).where(
                SourceItem.clip_id.is_not(None),
                SourceItem.source_metadata["channel_profile_id"].as_string()
                == str(channel_profile_id),
            )
        )
        if value is not None
    )
    ids.update(
        value
        for value in session.scalars(
            select(SourceItem.clip_id)
            .join(
                DiscoveryCandidate,
                DiscoveryCandidate.source_item_id == SourceItem.id,
            )
            .where(
                SourceItem.clip_id.is_not(None),
                DiscoveryCandidate.candidate_metadata[
                    "channel_profile_id"
                ].as_string()
                == str(channel_profile_id),
            )
        )
        if value is not None
    )
    from katcha.content_models import ContentItem
    ids.update(value for value in session.scalars(select(ContentItem.clip_id).where(
        ContentItem.channel_profile_id == channel_profile_id, ContentItem.clip_id.is_not(None)
    )) if value is not None)
    ids.update(value for value in session.scalars(select(SourceItem.clip_id).join(
        ContentItem, ContentItem.source_id == SourceItem.id
    ).where(ContentItem.channel_profile_id == channel_profile_id, SourceItem.clip_id.is_not(None)))
        if value is not None)
    return ids


def clips_for_channel(session: Session, channel_profile_id: uuid.UUID) -> list[Clip]:
    ids = clip_ids_for_channel(session, channel_profile_id)
    if not ids:
        return []
    return list(
        session.scalars(
            select(Clip)
            .where(Clip.id.in_(ids))
            .order_by(Clip.created_at.desc())
        )
    )


def clip_reference_summary(
    session: Session,
    clip_id: uuid.UUID,
) -> ClipReferenceSummary:
    productions = list(
        session.scalars(select(Production).where(Production.clip_id == clip_id))
    )
    episodes = list(
        session.scalars(
            select(ShortEpisode)
            .join(
                ShortEpisodeItem,
                ShortEpisodeItem.short_episode_id == ShortEpisode.id,
            )
            .where(ShortEpisodeItem.clip_id == clip_id)
        )
    )
    compilations = list(
        session.scalars(
            select(Compilation)
            .join(
                CompilationSegment,
                CompilationSegment.compilation_id == Compilation.id,
            )
            .where(CompilationSegment.clip_id == clip_id)
        )
    )
    active = sum(
        production.status in _ACTIVE_PRODUCTION_STATUSES
        for production in productions
    )
    active += sum(
        episode.status in _ACTIVE_EPISODE_STATUSES for episode in episodes
    )
    active += sum(
        compilation.status in _ACTIVE_COMPILATION_STATUSES
        for compilation in compilations
    )
    return ClipReferenceSummary(
        production_count=len(productions),
        short_episode_count=len(episodes),
        compilation_count=len(compilations),
        active_count=active,
    )


def ensure_lifecycle(session: Session, clip: Clip) -> ClipLifecycle:
    lifecycle = session.get(ClipLifecycle, clip.id)
    if lifecycle is None:
        lifecycle = ClipLifecycle(
            clip_id=clip.id,
            lifecycle_state="hot",
            tags=[],
            library_metadata={},
            search_document="",
            embedding_metadata={},
        )
        session.add(lifecycle)
        session.flush()
    return lifecycle


def _ai_search_fields(features: ClipFeature | None) -> list[str]:
    if features is None:
        return []
    ai = dict(features.ai_features or {})
    payload = ai.get("deep") or ai.get("bulk") or {}
    if not isinstance(payload, dict):
        payload = {}
    values: list[str] = []
    for key in (
        "event_summary",
        "setup",
        "payoff",
        "audio_relevance",
        "deep_video_reason",
    ):
        value = payload.get(key)
        if value:
            values.append(str(value))
    for key in ("categories", "tone", "best_commentary_moments", "timeline"):
        raw = payload.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if item)
    if features.transcript:
        values.append(features.transcript[:12000])
    return values


def refresh_search_document(
    session: Session,
    clip: Clip,
    lifecycle: ClipLifecycle | None = None,
) -> ClipLifecycle:
    lifecycle = lifecycle or ensure_lifecycle(session, clip)
    sources = list(
        session.scalars(select(SourceItem).where(SourceItem.clip_id == clip.id))
    )
    features = session.get(ClipFeature, clip.id)
    channels = channel_info_for_clip(session, clip.id)

    values: list[str] = [
        str(clip.id),
        clip.sha256,
        clip.status,
        lifecycle.lifecycle_state,
        *(lifecycle.tags or []),
    ]
    for key, value in dict(lifecycle.library_metadata or {}).items():
        if value not in (None, "", [], {}):
            values.append(str(key))
            if isinstance(value, (list, tuple, set)):
                values.extend(str(item) for item in value)
            else:
                values.append(str(value))
    for source in sources:
        values.extend(
            str(value)
            for value in (
                source.title,
                source.creator,
                source.platform,
                source.source_url,
                source.canonical_url,
            )
            if value
        )
    values.extend(item["name"] for item in channels)
    values.extend(_ai_search_fields(features))

    document = "\n".join(dict.fromkeys(value.strip() for value in values if value.strip()))
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    lifecycle.search_document = document
    previous = dict(lifecycle.embedding_metadata or {})
    if previous.get("document_sha256") != digest:
        lifecycle.embedding_metadata = {
            **previous,
            "schema": "clip-search-v1",
            "document_sha256": digest,
            "status": "pending",
            "stale": True,
        }
    return lifecycle


def update_library_metadata(
    clip_id: uuid.UUID,
    *,
    tags: list[str],
    library_metadata: dict[str, Any],
    actor: str = "operator",
) -> ClipLifecycle:
    normalized_tags = list(
        dict.fromkeys(
            value.strip().casefold()
            for value in tags
            if value and value.strip()
        )
    )[:50]
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise ValueError("clip not found")
        lifecycle = ensure_lifecycle(session, clip)
        lifecycle.tags = normalized_tags
        lifecycle.library_metadata = {
            str(key)[:80]: value
            for key, value in dict(library_metadata or {}).items()
            if value not in (None, "")
        }
        refresh_search_document(session, clip, lifecycle)
        session.add(
            DomainEvent(
                aggregate_type="clip",
                aggregate_id=str(clip.id),
                event_type="clip.library_metadata_updated",
                payload={
                    "clip_id": str(clip.id),
                    "tags": lifecycle.tags,
                    "actor": actor,
                    "embedding_reindex_required": True,
                },
            )
        )
        session.flush()
        session.refresh(lifecycle)
        session.expunge(lifecycle)
        return lifecycle


def _assert_not_active(
    session: Session,
    clip_id: uuid.UUID,
) -> ClipReferenceSummary:
    refs = clip_reference_summary(session, clip_id)
    if refs.active_count:
        raise ValueError(
            "clip is still required by an active production; finish or cancel that work first"
        )
    return refs


def archive_clip(
    clip_id: uuid.UUID,
    *,
    actor: str = "operator",
    reason: str | None = None,
    store: ObjectStore | None = None,
) -> ClipLifecycle:
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise ValueError("clip not found")
        lifecycle = ensure_lifecycle(session, clip)
        if lifecycle.lifecycle_state == "purged":
            raise ValueError("purged clip media cannot be archived")
        if lifecycle.lifecycle_state == "archived":
            session.expunge(lifecycle)
            return lifecycle
        _assert_not_active(session, clip.id)
        source_key = clip.storage_key
        archive_key = ObjectStore.archive_key(clip.sha256, clip.extension)

    object_store = store or ObjectStore()
    if object_store.exists(source_key):
        object_store.move(source_key, archive_key)
    elif not object_store.exists(archive_key):
        raise ValueError("stored clip media is missing")

    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise RuntimeError("clip disappeared during archive")
        lifecycle = ensure_lifecycle(session, clip)
        lifecycle.lifecycle_state = "archived"
        lifecycle.archive_key = archive_key
        lifecycle.archived_at = datetime.now(UTC)
        lifecycle.purged_at = None
        refresh_search_document(session, clip, lifecycle)
        session.add(
            DomainEvent(
                aggregate_type="clip",
                aggregate_id=str(clip.id),
                event_type="clip.archived",
                payload={
                    "clip_id": str(clip.id),
                    "archive_key": archive_key,
                    "actor": actor,
                    "reason": (reason or "")[:1000],
                },
            )
        )
        session.flush()
        session.refresh(lifecycle)
        session.expunge(lifecycle)
        return lifecycle


def restore_clip(
    clip_id: uuid.UUID,
    *,
    actor: str = "operator",
    store: ObjectStore | None = None,
) -> ClipLifecycle:
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise ValueError("clip not found")
        lifecycle = ensure_lifecycle(session, clip)
        if lifecycle.lifecycle_state == "purged":
            raise ValueError("purged clip media cannot be restored")
        if lifecycle.lifecycle_state == "hot":
            session.expunge(lifecycle)
            return lifecycle
        if not lifecycle.archive_key:
            raise ValueError("archived clip has no archive object key")
        archive_key = lifecycle.archive_key
        hot_key = clip.storage_key

    object_store = store or ObjectStore()
    if object_store.exists(archive_key):
        object_store.move(archive_key, hot_key)
    elif not object_store.exists(hot_key):
        raise ValueError("archived clip media is missing")

    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise RuntimeError("clip disappeared during restore")
        lifecycle = ensure_lifecycle(session, clip)
        lifecycle.lifecycle_state = "hot"
        lifecycle.archive_key = None
        lifecycle.archived_at = None
        lifecycle.purged_at = None
        refresh_search_document(session, clip, lifecycle)
        session.add(
            DomainEvent(
                aggregate_type="clip",
                aggregate_id=str(clip.id),
                event_type="clip.restored",
                payload={"clip_id": str(clip.id), "actor": actor},
            )
        )
        session.flush()
        session.refresh(lifecycle)
        session.expunge(lifecycle)
        return lifecycle


def purge_clip_media(
    clip_id: uuid.UUID,
    *,
    actor: str = "operator",
    reason: str | None = None,
    store: ObjectStore | None = None,
) -> ClipLifecycle:
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise ValueError("clip not found")
        lifecycle = ensure_lifecycle(session, clip)
        if lifecycle.lifecycle_state == "purged":
            session.expunge(lifecycle)
            return lifecycle
        refs = _assert_not_active(session, clip.id)
        features = session.get(ClipFeature, clip.id)
        keys = [clip.storage_key]
        if lifecycle.archive_key:
            keys.append(lifecycle.archive_key)
        if features is not None:
            if features.contact_sheet_key:
                keys.append(features.contact_sheet_key)
            keys.extend(features.keyframe_keys or [])

    object_store = store or ObjectStore()
    existing = [key for key in keys if object_store.exists(key)]
    object_store.delete_many(existing)

    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise RuntimeError("clip disappeared during purge")
        lifecycle = ensure_lifecycle(session, clip)
        lifecycle.lifecycle_state = "purged"
        lifecycle.archive_key = None
        lifecycle.purged_at = datetime.now(UTC)
        refresh_search_document(session, clip, lifecycle)
        session.add(
            DomainEvent(
                aggregate_type="clip",
                aggregate_id=str(clip.id),
                event_type="clip.media_purged",
                payload={
                    "clip_id": str(clip.id),
                    "actor": actor,
                    "reason": (reason or "")[:1000],
                    "retained_metadata": True,
                    "reference_count": (
                        refs.production_count
                        + refs.short_episode_count
                        + refs.compilation_count
                    ),
                },
            )
        )
        session.flush()
        session.refresh(lifecycle)
        session.expunge(lifecycle)
        return lifecycle


def _phash_distance(left: str, right: str) -> int:
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 64


def perceptual_similarity(
    left: ClipFeature | None,
    right: ClipFeature | None,
) -> float:
    left_hashes = list(left.perceptual_hashes or []) if left else []
    right_hashes = list(right.perceptual_hashes or []) if right else []
    count = min(len(left_hashes), len(right_hashes))
    if count < 3:
        return 0.0
    distances = [
        _phash_distance(left_hashes[index], right_hashes[index])
        for index in range(count)
    ]
    return max(0.0, 1.0 - (sum(distances) / (64.0 * count)))


def _duplicate_candidates(
    session: Session,
    clips: list[Clip],
) -> dict[uuid.UUID, tuple[uuid.UUID, float]]:
    features = {
        clip.id: session.get(ClipFeature, clip.id)
        for clip in clips
    }
    scores = {
        clip.id: float(features[clip.id].candidate_score or 0)
        if features[clip.id] is not None
        else 0.0
        for clip in clips
    }
    status_weight = {
        "measured": 12,
        "published": 11,
        "scheduled": 10,
        "review": 9,
        "rendered": 8,
        "voiced": 7,
        "scripted": 6,
        "selected": 5,
        "scored": 4,
        "analyzed": 3,
        "normalized": 2,
        "ingested": 1,
        "failed": 0,
    }
    duplicate_of: dict[uuid.UUID, tuple[uuid.UUID, float]] = {}
    for index, left in enumerate(clips):
        left_duration = float(left.duration_seconds or 0)
        for right in clips[index + 1 :]:
            right_duration = float(right.duration_seconds or 0)
            longest = max(left_duration, right_duration, 0.001)
            if abs(left_duration - right_duration) / longest > 0.18:
                continue
            similarity = perceptual_similarity(features[left.id], features[right.id])
            if similarity < _DUPLICATE_SIMILARITY:
                continue
            left_rank = (
                status_weight.get(left.status, 0),
                scores[left.id],
                -left.created_at.timestamp(),
            )
            right_rank = (
                status_weight.get(right.status, 0),
                scores[right.id],
                -right.created_at.timestamp(),
            )
            canonical, duplicate = (
                (left, right) if left_rank >= right_rank else (right, left)
            )
            existing = duplicate_of.get(duplicate.id)
            if existing is None or similarity > existing[1]:
                duplicate_of[duplicate.id] = (canonical.id, round(similarity, 4))
    return duplicate_of


def get_retention_policy(
    channel_profile_id: uuid.UUID,
) -> ClipRetentionPolicy | None:
    with session_scope() as session:
        row = session.scalar(
            select(ClipRetentionPolicy).where(
                ClipRetentionPolicy.channel_profile_id == channel_profile_id
            )
        )
        if row is not None:
            session.expunge(row)
        return row


def upsert_retention_policy(
    channel_profile_id: uuid.UUID,
    *,
    retention_mode: str,
    auto_archive: bool,
    auto_purge: bool,
    auto_remove_duplicates: bool,
    archive_after_days: int | None,
    purge_after_days: int | None,
    failed_purge_after_days: int | None,
    actor: str,
    auto_delete_confirmed: bool,
) -> ClipRetentionPolicy:
    if retention_mode not in {"indefinite", "managed"}:
        raise ValueError("invalid retention mode")
    for name, value in (
        ("archive_after_days", archive_after_days),
        ("purge_after_days", purge_after_days),
        ("failed_purge_after_days", failed_purge_after_days),
    ):
        if value is not None and value < 1:
            raise ValueError(f"{name} must be positive")
    if (
        archive_after_days is not None
        and purge_after_days is not None
        and purge_after_days <= archive_after_days
    ):
        raise ValueError("purge_after_days must be greater than archive_after_days")
    if auto_purge and not auto_delete_confirmed:
        raise ValueError("automatic deletion requires explicit destructive-action confirmation")
    if retention_mode == "indefinite":
        auto_archive = False
        auto_purge = False
        auto_remove_duplicates = False
        archive_after_days = None
        purge_after_days = None
        failed_purge_after_days = None

    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError("channel profile not found")
        row = session.scalar(
            select(ClipRetentionPolicy).where(
                ClipRetentionPolicy.channel_profile_id == channel_profile_id
            )
        )
        if row is None:
            row = ClipRetentionPolicy(channel_profile_id=channel_profile_id)
            session.add(row)
        row.retention_mode = retention_mode
        row.auto_archive = auto_archive
        row.auto_purge = auto_purge
        row.auto_remove_duplicates = auto_remove_duplicates
        row.archive_after_days = archive_after_days
        row.purge_after_days = purge_after_days
        row.failed_purge_after_days = failed_purge_after_days
        if auto_purge:
            row.confirmed_at = datetime.now(UTC)
            row.confirmed_by = actor
        else:
            row.confirmed_at = None
            row.confirmed_by = None
        row.policy_metadata = {
            **dict(row.policy_metadata or {}),
            "channel_name": _channel_name(profile),
            "safety_version": "clip-retention-v1",
        }
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="channel_profile",
                aggregate_id=str(channel_profile_id),
                event_type="channel_profile.clip_retention_updated",
                payload={
                    "channel_profile_id": str(channel_profile_id),
                    "retention_mode": row.retention_mode,
                    "auto_archive": row.auto_archive,
                    "auto_purge": row.auto_purge,
                    "auto_remove_duplicates": row.auto_remove_duplicates,
                    "archive_after_days": row.archive_after_days,
                    "purge_after_days": row.purge_after_days,
                    "failed_purge_after_days": row.failed_purge_after_days,
                    "actor": actor,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def _age_days(value: datetime | None, now: datetime) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0.0, (now - value).total_seconds() / 86400)


def preview_channel_maintenance(
    channel_profile_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    now = now or datetime.now(UTC)
    with session_scope() as session:
        profile = session.get(ChannelProfile, channel_profile_id)
        if profile is None:
            raise ValueError("channel profile not found")
        policy = session.scalar(
            select(ClipRetentionPolicy).where(
                ClipRetentionPolicy.channel_profile_id == channel_profile_id
            )
        )
        clips = clips_for_channel(session, channel_profile_id)
        duplicates = _duplicate_candidates(session, clips)
        actions: list[dict[str, object]] = []
        shared_skipped = 0
        active_skipped = 0

        for clip in clips:
            lifecycle = ensure_lifecycle(session, clip)
            channels = channel_ids_for_clip(session, clip.id)
            if channels != {channel_profile_id}:
                shared_skipped += 1
                continue
            refs = clip_reference_summary(session, clip.id)
            if refs.active_count:
                active_skipped += 1
                continue
            if policy is None or policy.retention_mode != "managed":
                continue

            age = _age_days(clip.updated_at or clip.created_at, now)
            if (
                clip.status == "failed"
                and policy.auto_purge
                and policy.failed_purge_after_days
                and age >= policy.failed_purge_after_days
                and lifecycle.lifecycle_state != "purged"
            ):
                actions.append(
                    {
                        "clip_id": str(clip.id),
                        "action": "purge",
                        "reason": "failed_retention_expired",
                        "age_days": round(age, 1),
                    }
                )
                continue

            duplicate = duplicates.get(clip.id)
            if (
                duplicate is not None
                and policy.auto_remove_duplicates
                and lifecycle.lifecycle_state != "purged"
            ):
                canonical_id, similarity = duplicate
                actions.append(
                    {
                        "clip_id": str(clip.id),
                        "action": "purge" if policy.auto_purge else "archive",
                        "reason": "near_duplicate",
                        "canonical_clip_id": str(canonical_id),
                        "similarity": similarity,
                    }
                )
                continue

            if (
                lifecycle.lifecycle_state == "hot"
                and policy.auto_archive
                and policy.archive_after_days
                and clip.status in {"published", "measured"}
                and age >= policy.archive_after_days
            ):
                actions.append(
                    {
                        "clip_id": str(clip.id),
                        "action": "archive",
                        "reason": "published_retention_expired",
                        "age_days": round(age, 1),
                    }
                )
                continue

            if (
                lifecycle.lifecycle_state == "archived"
                and policy.auto_purge
                and policy.purge_after_days
                and _age_days(lifecycle.archived_at, now) >= policy.purge_after_days
            ):
                actions.append(
                    {
                        "clip_id": str(clip.id),
                        "action": "purge",
                        "reason": "archive_retention_expired",
                        "age_days": round(_age_days(lifecycle.archived_at, now), 1),
                    }
                )

        return {
            "channel_profile_id": str(channel_profile_id),
            "channel_name": _channel_name(profile),
            "policy_enabled": bool(policy and policy.retention_mode == "managed"),
            "clip_count": len(clips),
            "actions": actions,
            "archive_count": sum(item["action"] == "archive" for item in actions),
            "purge_count": sum(item["action"] == "purge" for item in actions),
            "duplicate_count": len(duplicates),
            "shared_skipped": shared_skipped,
            "active_skipped": active_skipped,
        }


def run_channel_maintenance(
    channel_profile_id: uuid.UUID,
    *,
    actor: str = "katcha-lifecycle",
    now: datetime | None = None,
) -> dict[str, object]:
    preview = preview_channel_maintenance(channel_profile_id, now=now)
    completed: list[dict[str, object]] = []
    blocked: list[dict[str, object]] = []
    for item in preview["actions"]:
        clip_id = uuid.UUID(str(item["clip_id"]))
        try:
            if item["action"] == "archive":
                archive_clip(
                    clip_id,
                    actor=actor,
                    reason=str(item["reason"]),
                )
            elif item["action"] == "purge":
                purge_clip_media(
                    clip_id,
                    actor=actor,
                    reason=str(item["reason"]),
                )
            completed.append(item)
        except (ValueError, RuntimeError) as exc:
            blocked.append(
                {
                    **item,
                    "error": str(exc)[:500],
                }
            )
    return {
        **preview,
        "completed": completed,
        "blocked": blocked,
    }
