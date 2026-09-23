from __future__ import annotations

import uuid
from dataclasses import dataclass
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
from katcha.db import session_scope
from katcha.discovery_trend_models import TrendReviewQueueItem
from katcha.domain import DiscoveryRunStatus
from katcha.models import DomainEvent
from katcha.services.trends import register_signal
from katcha.trend_models import TrendSignal


@dataclass(frozen=True, slots=True)
class DiscoverySignalSpec:
    discovery_observation_id: uuid.UUID
    discovery_candidate_id: uuid.UUID
    discovery_run_id: uuid.UUID
    topic: str
    provider_key: str
    external_id: str
    source_kind: str
    independence_key: str
    observed_at: datetime
    observation_key: str
    canonical_url: str
    source_name: str | None
    title: str | None
    body_excerpt: str | None
    author: str | None
    community: str | None
    language: str | None
    region: str | None
    published_at: datetime | None
    metrics: dict[str, float]
    media_refs: list[dict[str, Any]]
    aliases: list[str]
    tags: list[str]
    metadata: dict[str, Any]
    match_confidence: float
    match_reasons: list[str]


@dataclass(frozen=True, slots=True)
class TrendBridgeResult:
    scope: str
    signal_count: int
    observation_count: int
    candidate_count: int
    topic_count: int
    signal_ids: tuple[uuid.UUID, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _metrics(metadata: dict[str, Any]) -> dict[str, float]:
    raw = metadata.get("source_metrics")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def _host(value: str | None) -> str | None:
    if not value:
        return None
    host = (urlparse(value).hostname or "").strip().casefold()
    return host or None


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _completed_run(run: DiscoveryRun) -> DiscoveryRun:
    if run.status != DiscoveryRunStatus.COMPLETED.value:
        raise ValueError(
            f"discovery run is not completed: {run.id} ({run.status})"
        )
    return run


def source_independence_key(
    candidate: DiscoveryCandidate,
    observation_metadata: dict[str, Any],
) -> str:
    channel_id = _clean_text(observation_metadata.get("channel_id")) or _clean_text(
        (candidate.provenance_claims or {}).get("channel_id")
    )
    if channel_id:
        return f"youtube-channel:{channel_id.casefold()}"

    if candidate.adapter_key == "reddit":
        subreddit = _clean_text(observation_metadata.get("subreddit"))
        if subreddit:
            return f"reddit-subreddit:{subreddit.casefold()}"
        if candidate.creator:
            return f"reddit-user:{candidate.creator.strip().casefold()}"

    feed_url = _clean_text(observation_metadata.get("feed_url")) or _clean_text(
        (candidate.provenance_claims or {}).get("feed_url")
    )
    if feed_url:
        return f"feed:{feed_url.casefold()}"

    if candidate.creator_url:
        return f"creator-url:{candidate.creator_url.strip().casefold()}"
    if candidate.creator:
        return f"creator:{candidate.creator.strip().casefold()}"
    host = _host(candidate.canonical_url) or _host(candidate.source_url)
    if host:
        return f"host:{host}"
    return f"candidate:{candidate.id}"


def topic_for_discovery(
    candidate: DiscoveryCandidate,
    *,
    queue_metadata: dict[str, Any] | None = None,
    watch: TopicWatchVersion | None = None,
    run: DiscoveryRun | None = None,
) -> str:
    queue_metadata = queue_metadata or {}
    cluster_label = _clean_text(queue_metadata.get("cluster_label"))
    if cluster_label:
        return cluster_label
    if candidate.title and candidate.title.strip():
        return candidate.title.strip()
    if run is not None:
        query = dict(run.query or {})
        explicit = _clean_text(query.get("q"))
        if explicit:
            return explicit
        include_terms = [
            str(term).strip()
            for term in query.get("include_terms", [])
            if str(term).strip()
        ]
        if include_terms:
            return " ".join(include_terms)
    if watch is not None and watch.include_terms:
        terms = [str(term).strip() for term in watch.include_terms if str(term).strip()]
        if terms:
            return " ".join(terms)
    if candidate.canonical_url:
        return candidate.canonical_url
    host = _host(candidate.source_url)
    return host or f"discovery-{candidate.id}"


def _language_region(
    observation_metadata: dict[str, Any],
    *,
    watch: TopicWatchVersion | None,
    run: DiscoveryRun | None,
) -> tuple[str | None, str | None]:
    language = _clean_text(observation_metadata.get("language"))
    region = _clean_text(
        observation_metadata.get("region") or observation_metadata.get("region_code")
    )
    if watch is not None:
        language = language or watch.language
        if region is None and watch.locale:
            locale_region = watch.locale.split("-")[-1].strip()
            if len(locale_region) == 2:
                region = locale_region
    if run is not None:
        query = dict(run.query or {})
        language = language or _clean_text(query.get("relevance_language"))
        region = region or _clean_text(query.get("region_code"))
    return (
        language.casefold() if language else None,
        region.casefold() if region else None,
    )


def build_discovery_signal_spec(
    candidate: DiscoveryCandidate,
    observation: DiscoveryObservation,
    *,
    queue_item: TrendReviewQueueItem | None = None,
    watch: TopicWatchVersion | None = None,
    run: DiscoveryRun | None = None,
) -> DiscoverySignalSpec:
    observation_metadata = dict(observation.observation_metadata or {})
    queue_metadata = dict(queue_item.queue_metadata or {}) if queue_item is not None else {}
    topic = topic_for_discovery(
        candidate,
        queue_metadata=queue_metadata,
        watch=watch,
        run=run,
    )
    published_at = _parse_datetime(observation_metadata.get("published_at"))
    source_name = (
        _clean_text(observation_metadata.get("feed_title"))
        or candidate.creator
        or candidate.adapter_key
    )
    body_excerpt = _clean_text(observation_metadata.get("description")) or _clean_text(
        observation_metadata.get("summary")
    )
    community = _clean_text(observation_metadata.get("subreddit"))
    language, region = _language_region(
        observation_metadata,
        watch=watch,
        run=run,
    )
    aliases = [
        str(value).strip()
        for value in queue_metadata.get("cluster_shared_tokens", [])
        if str(value).strip()
    ]
    tags = list(
        dict.fromkeys(
            [
                *(str(value).strip() for value in (watch.include_terms if watch else [])),
                candidate.adapter_key,
                candidate.platform,
            ]
        )
    )
    media_ref: dict[str, Any] = {
        "kind": "discovery_candidate",
        "discovery_candidate_id": str(candidate.id),
        "source_url": candidate.source_url,
        "canonical_url": candidate.canonical_url,
        "adapter_key": candidate.adapter_key,
        "platform": candidate.platform,
        "reuse_permission": "not_inferred",
    }
    outbound_url = _clean_text(observation_metadata.get("outbound_url"))
    if outbound_url:
        media_ref["outbound_url"] = outbound_url

    lineage: dict[str, Any] = {
        "discovery_candidate_id": str(candidate.id),
        "discovery_observation_id": str(observation.id),
        "discovery_run_id": str(observation.discovery_run_id),
        "provenance_confidence": float(candidate.provenance_confidence or 0),
        "provenance_claims": dict(candidate.provenance_claims or {}),
        "rights_inferred": False,
    }
    if watch is not None:
        lineage["topic_watch_id"] = str(watch.id)
        lineage["topic_watch_key"] = watch.watch_key
        lineage["topic_watch_version"] = watch.version
    if queue_item is not None:
        lineage.update(
            {
                "trend_review_queue_item_id": str(queue_item.id),
                "queue_key": queue_item.queue_key,
                "trend_score_id": str(queue_item.trend_score_id),
                "cluster_key": queue_metadata.get("cluster_key"),
                "cluster_label": queue_metadata.get("cluster_label"),
                "cluster_member_ids": queue_metadata.get("cluster_member_ids", []),
                "cluster_source_keys": queue_metadata.get("cluster_source_keys", []),
            }
        )

    cluster_assigned = bool(_clean_text(queue_metadata.get("cluster_label")))
    return DiscoverySignalSpec(
        discovery_observation_id=observation.id,
        discovery_candidate_id=candidate.id,
        discovery_run_id=observation.discovery_run_id,
        topic=topic,
        provider_key=candidate.adapter_key,
        external_id=candidate.external_id or str(candidate.id),
        source_kind=candidate.adapter_key,
        independence_key=source_independence_key(candidate, observation_metadata),
        observed_at=_utc(observation.observed_at),
        observation_key=f"discovery-observation:{observation.id}",
        canonical_url=candidate.canonical_url,
        source_name=source_name,
        title=candidate.title,
        body_excerpt=body_excerpt,
        author=candidate.creator,
        community=community,
        language=language,
        region=region,
        published_at=published_at,
        metrics=_metrics(observation_metadata),
        media_refs=[media_ref],
        aliases=aliases,
        tags=[value for value in tags if value],
        metadata=lineage,
        match_confidence=0.95 if cluster_assigned else 0.75,
        match_reasons=[
            "discovery_queue_cluster"
            if cluster_assigned
            else "discovery_deterministic_fallback"
        ],
    )


def _register_specs(specs: list[DiscoverySignalSpec]) -> tuple[uuid.UUID, ...]:
    signal_ids: list[uuid.UUID] = []
    for spec in specs:
        signal = register_signal(
            topic=spec.topic,
            provider_key=spec.provider_key,
            external_id=spec.external_id,
            source_kind=spec.source_kind,
            independence_key=spec.independence_key,
            observed_at=spec.observed_at,
            observation_key=spec.observation_key,
            canonical_url=spec.canonical_url,
            source_name=spec.source_name,
            title=spec.title,
            body_excerpt=spec.body_excerpt,
            author=spec.author,
            community=spec.community,
            language=spec.language,
            region=spec.region,
            published_at=spec.published_at,
            metrics=spec.metrics,
            media_refs=spec.media_refs,
            aliases=spec.aliases,
            tags=spec.tags,
            metadata=spec.metadata,
            match_confidence=spec.match_confidence,
            match_reasons=spec.match_reasons,
        )
        signal_ids.append(signal.id)
    return tuple(signal_ids)


def _result(scope: str, specs: list[DiscoverySignalSpec], signal_ids: tuple[uuid.UUID, ...]) -> TrendBridgeResult:
    result = TrendBridgeResult(
        scope=scope,
        signal_count=len(signal_ids),
        observation_count=len(specs),
        candidate_count=len({spec.discovery_candidate_id for spec in specs}),
        topic_count=len({spec.topic.casefold() for spec in specs}),
        signal_ids=signal_ids,
    )
    _emit_bridge_event(result)
    return result


def bridge_discovery_run(discovery_run_id: uuid.UUID) -> TrendBridgeResult:
    with session_scope() as session:
        run = session.get(DiscoveryRun, discovery_run_id)
        if run is None:
            raise ValueError(f"discovery run not found: {discovery_run_id}")
        _completed_run(run)
        watch: TopicWatchVersion | None = None
        raw_watch_id = (run.run_metadata or {}).get("topic_watch_id")
        if raw_watch_id:
            try:
                watch = session.get(TopicWatchVersion, uuid.UUID(str(raw_watch_id)))
            except ValueError:
                watch = None
        rows = list(
            session.execute(
                select(DiscoveryObservation, DiscoveryCandidate)
                .join(
                    DiscoveryCandidate,
                    DiscoveryObservation.discovery_candidate_id == DiscoveryCandidate.id,
                )
                .where(DiscoveryObservation.discovery_run_id == discovery_run_id)
                .order_by(DiscoveryObservation.observed_at, DiscoveryObservation.id)
            )
        )
        specs = [
            build_discovery_signal_spec(
                candidate,
                observation,
                watch=watch,
                run=run,
            )
            for observation, candidate in rows
        ]

    signal_ids = _register_specs(specs)
    return _result(f"run:{discovery_run_id}", specs, signal_ids)


def bridge_topic_watch_queue(
    topic_watch_id: uuid.UUID,
    *,
    queue_key: str | None = None,
) -> TrendBridgeResult:
    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        selected_queue_key = queue_key.strip() if queue_key and queue_key.strip() else None
        if selected_queue_key is None:
            selected_queue_key = session.scalar(
                select(TrendReviewQueueItem.queue_key)
                .where(TrendReviewQueueItem.topic_watch_id == topic_watch_id)
                .order_by(TrendReviewQueueItem.created_at.desc())
                .limit(1)
            )
        if not selected_queue_key:
            raise ValueError("topic watch has no review queue to bridge")
        queue_items = list(
            session.scalars(
                select(TrendReviewQueueItem)
                .where(
                    TrendReviewQueueItem.topic_watch_id == topic_watch_id,
                    TrendReviewQueueItem.queue_key == selected_queue_key,
                )
                .order_by(TrendReviewQueueItem.rank)
            )
        )
        if not queue_items:
            raise ValueError("topic watch review queue is empty")

        specs: list[DiscoverySignalSpec] = []
        incomplete_run_ids: set[uuid.UUID] = set()
        for item in queue_items:
            candidate = session.get(DiscoveryCandidate, item.discovery_candidate_id)
            if candidate is None:
                continue
            raw_run_ids = (item.queue_metadata or {}).get("discovery_run_ids", [])
            run_ids: list[uuid.UUID] = []
            for raw in raw_run_ids:
                try:
                    run_ids.append(uuid.UUID(str(raw)))
                except ValueError as exc:
                    raise ValueError(
                        f"trend review queue contains an invalid discovery run id: {raw}"
                    ) from exc
            stmt = select(DiscoveryObservation).where(
                DiscoveryObservation.discovery_candidate_id == candidate.id
            )
            if run_ids:
                stmt = stmt.where(DiscoveryObservation.discovery_run_id.in_(run_ids))
            observations = list(
                session.scalars(
                    stmt.order_by(DiscoveryObservation.observed_at, DiscoveryObservation.id)
                )
            )
            for observation in observations:
                run = session.get(DiscoveryRun, observation.discovery_run_id)
                if run is None:
                    raise ValueError(
                        f"discovery run not found: {observation.discovery_run_id}"
                    )
                if run.status != DiscoveryRunStatus.COMPLETED.value:
                    incomplete_run_ids.add(run.id)
                    continue
                specs.append(
                    build_discovery_signal_spec(
                        candidate,
                        observation,
                        queue_item=item,
                        watch=watch,
                        run=run,
                    )
                )
        if incomplete_run_ids:
            joined = ", ".join(str(value) for value in sorted(incomplete_run_ids, key=str))
            raise ValueError(
                "trend review queue references discovery runs that are not completed: "
                f"{joined}"
            )

    signal_ids = _register_specs(specs)
    scope = f"queue:{topic_watch_id}:{selected_queue_key}"
    return _result(scope, specs, signal_ids)


def bridged_signals(
    *,
    discovery_run_id: uuid.UUID | None = None,
    discovery_candidate_id: uuid.UUID | None = None,
    queue_key: str | None = None,
    limit: int = 100,
) -> list[TrendSignal]:
    if discovery_run_id is None and discovery_candidate_id is None and not queue_key:
        raise ValueError("at least one discovery lineage selector is required")
    with session_scope() as session:
        candidates = list(
            session.scalars(
                select(TrendSignal)
                .where(TrendSignal.observation_key.like("discovery-observation:%"))
                .order_by(TrendSignal.observed_at.desc())
                .limit(min(max(limit * 10, 100), 2000))
            )
        )
        result: list[TrendSignal] = []
        for signal in candidates:
            lineage = dict(signal.signal_metadata or {})
            if (
                discovery_run_id is not None
                and lineage.get("discovery_run_id") != str(discovery_run_id)
            ):
                continue
            if (
                discovery_candidate_id is not None
                and lineage.get("discovery_candidate_id") != str(discovery_candidate_id)
            ):
                continue
            if queue_key and lineage.get("queue_key") != queue_key:
                continue
            result.append(signal)
            if len(result) >= limit:
                break
        return result


def _emit_bridge_event(result: TrendBridgeResult) -> None:
    with session_scope() as session:
        session.add(
            DomainEvent(
                aggregate_type="discovery_trend_bridge",
                aggregate_id=result.scope,
                event_type="trend.discovery_bridged",
                payload={
                    "scope": result.scope,
                    "signal_count": result.signal_count,
                    "observation_count": result.observation_count,
                    "candidate_count": result.candidate_count,
                    "topic_count": result.topic_count,
                },
            )
        )
