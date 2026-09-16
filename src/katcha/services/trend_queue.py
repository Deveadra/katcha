from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from katcha.acquisition.clustering import ClusterCandidate, TrendCluster, cluster_candidates
from katcha.acquisition_models import (
    CandidateTrendScore,
    DiscoveryCandidate,
    DiscoveryObservation,
    TopicWatchVersion,
)
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.services.trends import compute_candidate_trend_score
from katcha.trend_models import TrendReviewQueueItem

_QUEUE_STATUSES = {"pending", "selected", "skipped"}


@dataclass(frozen=True, slots=True)
class QueueMaterializationResult:
    topic_watch_id: uuid.UUID
    queue_key: str
    candidate_count: int
    cluster_count: int
    queue_count: int
    reused: bool


def _cluster_map(
    candidates: list[DiscoveryCandidate],
) -> tuple[dict[str, TrendCluster], int]:
    clusters = cluster_candidates(
        [
            ClusterCandidate(
                candidate_id=str(candidate.id),
                title=candidate.title or candidate.source_url,
                source_key=candidate.adapter_key,
            )
            for candidate in candidates
        ],
        threshold=0.4,
    )
    mapping: dict[str, TrendCluster] = {}
    for cluster in clusters:
        for candidate_id in cluster.member_ids:
            mapping[candidate_id] = cluster
    return mapping, len(clusters)


def materialize_trend_review_queue(
    topic_watch_id: uuid.UUID,
    *,
    queue_key: str,
    discovery_run_ids: list[uuid.UUID],
    top_n: int = 25,
) -> QueueMaterializationResult:
    key = queue_key.strip()
    if not key:
        raise ValueError("queue_key is required")
    if len(key) > 160:
        raise ValueError("queue_key must be 160 characters or fewer")
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    unique_run_ids = sorted(set(discovery_run_ids), key=str)
    if not unique_run_ids:
        return QueueMaterializationResult(
            topic_watch_id=topic_watch_id,
            queue_key=key,
            candidate_count=0,
            cluster_count=0,
            queue_count=0,
            reused=False,
        )

    with session_scope() as session:
        watch = session.get(TopicWatchVersion, topic_watch_id)
        if watch is None:
            raise ValueError(f"topic watch version not found: {topic_watch_id}")
        existing = list(
            session.scalars(
                select(TrendReviewQueueItem)
                .where(
                    TrendReviewQueueItem.topic_watch_id == topic_watch_id,
                    TrendReviewQueueItem.queue_key == key,
                )
                .order_by(TrendReviewQueueItem.rank)
            )
        )
        if existing:
            return QueueMaterializationResult(
                topic_watch_id=topic_watch_id,
                queue_key=key,
                candidate_count=len(existing),
                cluster_count=len(
                    {
                        str(item.queue_metadata.get("cluster_key") or "")
                        for item in existing
                    }
                ),
                queue_count=len(existing),
                reused=True,
            )

        candidate_ids = list(
            session.scalars(
                select(DiscoveryObservation.discovery_candidate_id)
                .where(DiscoveryObservation.discovery_run_id.in_(unique_run_ids))
                .distinct()
            )
        )
        candidates = list(
            session.scalars(
                select(DiscoveryCandidate)
                .where(DiscoveryCandidate.id.in_(candidate_ids))
                .order_by(DiscoveryCandidate.id)
            )
        )
        max_queue = min(top_n, watch.max_candidates)

    cluster_by_candidate, cluster_count = _cluster_map(candidates)
    scored: list[tuple[DiscoveryCandidate, CandidateTrendScore, TrendCluster]] = []
    for candidate in candidates:
        cluster = cluster_by_candidate[str(candidate.id)]
        score = compute_candidate_trend_score(
            candidate.id,
            topic_watch_id,
            score_key=f"final:{key}",
            related_item_count=max(cluster.member_count - 1, 0),
            corroborating_source_count=cluster.source_count,
        )
        scored.append((candidate, score, cluster))
    scored.sort(key=lambda item: (-float(item[1].score), str(item[0].id)))
    selected = scored[:max_queue]

    with session_scope() as session:
        for rank, (candidate, score, cluster) in enumerate(selected, start=1):
            session.add(
                TrendReviewQueueItem(
                    topic_watch_id=topic_watch_id,
                    discovery_candidate_id=candidate.id,
                    trend_score_id=score.id,
                    queue_key=key,
                    rank=rank,
                    status="pending",
                    queue_metadata={
                        "cluster_key": cluster.cluster_key,
                        "cluster_label": cluster.label,
                        "cluster_member_ids": list(cluster.member_ids),
                        "cluster_source_keys": list(cluster.source_keys),
                        "cluster_shared_tokens": list(cluster.shared_tokens),
                        "related_item_count": max(cluster.member_count - 1, 0),
                        "corroborating_source_count": cluster.source_count,
                        "discovery_run_ids": [str(run_id) for run_id in unique_run_ids],
                    },
                )
            )
        session.add(
            DomainEvent(
                aggregate_type="topic_watch",
                aggregate_id=str(topic_watch_id),
                event_type="topic_watch.review_queue_materialized",
                payload={
                    "topic_watch_id": str(topic_watch_id),
                    "queue_key": key,
                    "candidate_count": len(candidates),
                    "cluster_count": cluster_count,
                    "queue_count": len(selected),
                    "run_count": len(unique_run_ids),
                },
            )
        )

    return QueueMaterializationResult(
        topic_watch_id=topic_watch_id,
        queue_key=key,
        candidate_count=len(candidates),
        cluster_count=cluster_count,
        queue_count=len(selected),
        reused=False,
    )


def update_trend_review_queue_status(
    item_id: uuid.UUID,
    *,
    status: str,
    actor: str = "operator",
    note: str | None = None,
) -> TrendReviewQueueItem:
    normalized = status.strip().casefold()
    if normalized not in _QUEUE_STATUSES:
        raise ValueError(
            f"queue status must be one of: {', '.join(sorted(_QUEUE_STATUSES))}"
        )
    with session_scope() as session:
        item = session.get(TrendReviewQueueItem, item_id)
        if item is None:
            raise ValueError(f"trend review queue item not found: {item_id}")
        previous = item.status
        item.status = normalized
        item.queue_metadata = {
            **dict(item.queue_metadata or {}),
            "last_decision_actor": actor,
            "last_decision_note": note,
        }
        session.add(
            DomainEvent(
                aggregate_type="trend_review_queue_item",
                aggregate_id=str(item.id),
                event_type="trend_review_queue_item.status_changed",
                payload={
                    "trend_review_queue_item_id": str(item.id),
                    "topic_watch_id": str(item.topic_watch_id),
                    "discovery_candidate_id": str(item.discovery_candidate_id),
                    "queue_key": item.queue_key,
                    "previous_status": previous,
                    "status": normalized,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(item)
        session.expunge(item)
        return item
