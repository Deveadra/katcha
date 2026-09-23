from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from katcha.acquisition_models import (
    DiscoveryCandidate,
    DiscoveryObservation,
    DiscoveryRun,
    TopicWatchVersion,
)
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.discovery_trend_models import TrendReviewQueueItem
from katcha.services.trends import register_signal


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _float_metrics(metadata: dict[str, Any], adapter_key: str) -> dict[str, float]:
    raw = metadata.get("source_metrics")
    result: dict[str, float] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key) in {"search_rank", "feed_rank"}:
                continue
            try:
                result[str(key)] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    if adapter_key == "rss_atom":
        result["mentions"] = max(1.0, result.get("mentions", 0.0))
    return result


def _independence_key(candidate: DiscoveryCandidate) -> str:
    metadata = dict(candidate.candidate_metadata or {})
    if candidate.adapter_key == "youtube":
        channel_id = str(metadata.get("channel_id") or "").strip()
        if channel_id:
            return f"youtube:{channel_id.casefold()}"
    if candidate.adapter_key == "reddit":
        subreddit = str(metadata.get("subreddit") or "").strip()
        if subreddit:
            return f"reddit:{subreddit.casefold()}"
    if candidate.adapter_key == "rss_atom":
        feed_url = str(metadata.get("feed_url") or candidate.source_url)
        host = (urlparse(feed_url).hostname or "unknown").casefold()
        return f"rss_atom:{host}"
    creator = (candidate.creator or "unknown").strip().casefold()
    return f"{candidate.adapter_key}:{creator}"


def _observation_key(
    *,
    provider_key: str,
    external_id: str,
    observed_at: datetime,
    metrics: dict[str, float],
    canonical_url: str,
) -> str:
    settings = get_settings()
    bucket = int(observed_at.timestamp()) // settings.trend_observation_bucket_seconds
    payload = json.dumps(
        {
            "provider_key": provider_key,
            "external_id": external_id,
            "bucket": bucket,
            "metrics": metrics,
            "canonical_url": canonical_url,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _latest_observation(
    candidate_id: uuid.UUID,
    run_ids: list[uuid.UUID],
) -> tuple[DiscoveryObservation, DiscoveryRun] | None:
    with session_scope() as session:
        stmt = (
            select(DiscoveryObservation, DiscoveryRun)
            .join(
                DiscoveryRun,
                DiscoveryObservation.discovery_run_id == DiscoveryRun.id,
            )
            .where(DiscoveryObservation.discovery_candidate_id == candidate_id)
            .order_by(DiscoveryObservation.observed_at.desc())
            .limit(1)
        )
        if run_ids:
            stmt = stmt.where(DiscoveryObservation.discovery_run_id.in_(run_ids))
        return session.execute(stmt).first()


def bridge_topic_watch_queue_to_trend_signals(
    topic_watch_id: uuid.UUID,
    *,
    queue_key: str,
) -> dict[str, Any]:
    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        rows = list(
            session.execute(
                select(TrendReviewQueueItem, DiscoveryCandidate)
                .join(
                    DiscoveryCandidate,
                    TrendReviewQueueItem.discovery_candidate_id == DiscoveryCandidate.id,
                )
                .where(
                    TrendReviewQueueItem.topic_watch_id == topic_watch_id,
                    TrendReviewQueueItem.queue_key == queue_key,
                )
                .order_by(TrendReviewQueueItem.rank)
            )
        )
        aliases = list(watch.include_terms or [])
        language = watch.language
        locale = watch.locale
        channel_profile_id = watch.channel_profile_id

    signal_ids: set[str] = set()
    topic_labels: set[str] = set()
    for queue_item, candidate in rows:
        queue_metadata = dict(queue_item.queue_metadata or {})
        raw_run_ids = queue_metadata.get("discovery_run_ids") or []
        run_ids: list[uuid.UUID] = []
        for raw_run_id in raw_run_ids:
            try:
                run_ids.append(uuid.UUID(str(raw_run_id)))
            except ValueError:
                continue
        observation_row = _latest_observation(candidate.id, run_ids)
        if observation_row is None:
            continue
        observation, run = observation_row
        metadata = dict(observation.observation_metadata or {})
        metrics = _float_metrics(metadata, candidate.adapter_key)
        external_id = candidate.external_id or candidate.canonical_url
        label = str(queue_metadata.get("cluster_label") or candidate.title or watch.name).strip()
        if not label:
            continue
        topic_labels.add(label)
        shared_tokens = [
            str(value)
            for value in (queue_metadata.get("cluster_shared_tokens") or [])
            if str(value).strip()
        ]
        body_excerpt = str(
            metadata.get("description") or metadata.get("summary") or ""
        ).strip() or None
        community = (
            str(metadata.get("subreddit") or "").strip() or None
            if candidate.adapter_key == "reddit"
            else None
        )
        media_refs = [
            {
                "kind": "discovery_candidate",
                "url": candidate.source_url,
                "rights_status": "unassessed",
                "discovery_candidate_id": str(candidate.id),
            }
        ]
        signal = register_signal(
            topic=label,
            provider_key=candidate.adapter_key,
            external_id=external_id,
            source_kind=candidate.adapter_key,
            independence_key=_independence_key(candidate),
            observed_at=observation.observed_at,
            observation_key=_observation_key(
                provider_key=candidate.adapter_key,
                external_id=external_id,
                observed_at=observation.observed_at,
                metrics=metrics,
                canonical_url=candidate.canonical_url,
            ),
            canonical_url=candidate.canonical_url,
            source_name=candidate.creator,
            title=candidate.title,
            body_excerpt=body_excerpt,
            author=candidate.creator,
            community=community,
            language=language,
            region=locale,
            published_at=_parse_datetime(metadata.get("published_at")),
            metrics=metrics,
            media_refs=media_refs,
            aliases=aliases,
            tags=[watch.watch_key, *shared_tokens],
            metadata={
                "discovery_candidate_id": str(candidate.id),
                "discovery_observation_id": str(observation.id),
                "discovery_run_id": str(run.id),
                "provenance_confidence": float(candidate.provenance_confidence),
                "provenance_claims": dict(candidate.provenance_claims or {}),
            },
            match_confidence=float(candidate.provenance_confidence),
            match_reasons=["deterministic_story_cluster", "discovery_observation"],
        )
        signal_ids.add(str(signal.id))

    return {
        "topic_watch_id": str(topic_watch_id),
        "channel_profile_id": str(channel_profile_id) if channel_profile_id else None,
        "queue_key": queue_key,
        "signals_bridged": len(signal_ids),
        "story_topics": len(topic_labels),
    }
