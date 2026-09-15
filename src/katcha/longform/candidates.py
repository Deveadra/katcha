from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.config import Settings
from katcha.longform.schemas import CandidateEvidence
from katcha.longform.scoring import CandidateSignals, score_pool, sequence_pool
from katcha.models import Clip, ClipFeature, SourceItem
from katcha.production_models import Production
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot

_STOP_WORDS = {
    "about",
    "after",
    "all",
    "and",
    "best",
    "compilation",
    "clips",
    "funny",
    "most",
    "of",
    "the",
    "that",
    "this",
    "video",
    "videos",
    "with",
}


def _active_ai_features(features: ClipFeature) -> dict[str, Any]:
    ai = dict(features.ai_features or {})
    deep = ai.get("deep")
    bulk = ai.get("bulk")
    if isinstance(deep, dict):
        return deep
    if isinstance(bulk, dict):
        return bulk
    return {}


def _text_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _theme_tokens(theme: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", theme.casefold())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def _theme_match(theme: str, candidate: CandidateEvidence) -> bool:
    wanted = _theme_tokens(theme)
    if not wanted:
        return True
    haystack = " ".join(
        [candidate.event_summary, *candidate.categories, *candidate.tone]
    ).casefold()
    tokens = set(re.findall(r"[a-z0-9]+", haystack))
    return bool(wanted & tokens)


def _latest_snapshots(
    session: Session,
    publication_ids: list[uuid.UUID],
) -> dict[uuid.UUID, PublicationAnalyticsSnapshot]:
    if not publication_ids:
        return {}
    snapshots = list(
        session.scalars(
            select(PublicationAnalyticsSnapshot)
            .where(PublicationAnalyticsSnapshot.publication_id.in_(publication_ids))
            .order_by(PublicationAnalyticsSnapshot.sampled_at.desc())
        )
    )
    latest: dict[uuid.UUID, PublicationAnalyticsSnapshot] = {}
    for snapshot in snapshots:
        latest.setdefault(snapshot.publication_id, snapshot)
    return latest


def select_compilation_candidates(
    session: Session,
    *,
    theme: str,
    target_duration_seconds: int,
    target_segment_count: int | None,
    settings: Settings,
) -> list[CandidateEvidence]:
    rows = list(
        session.execute(
            select(ClipFeature, Clip)
            .join(Clip, Clip.id == ClipFeature.clip_id)
            .where(
                ClipFeature.candidate_score.is_not(None),
                Clip.duration_seconds.is_not(None),
                Clip.duration_seconds > 0,
            )
            .order_by(ClipFeature.candidate_score.desc())
            .limit(settings.longform_candidate_pool_limit)
        )
    )
    if len(rows) < settings.longform_min_segments:
        raise ValueError(
            "not enough scored clips for a compilation: "
            f"need {settings.longform_min_segments}, found {len(rows)}"
        )

    clip_ids = [clip.id for _features, clip in rows]
    sources = list(
        session.scalars(
            select(SourceItem)
            .where(SourceItem.clip_id.in_(clip_ids))
            .order_by(SourceItem.discovered_at.asc())
        )
    )
    creator_by_clip: dict[uuid.UUID, str] = {}
    for source in sources:
        if source.clip_id and source.creator:
            creator_by_clip.setdefault(source.clip_id, source.creator)

    productions = list(
        session.scalars(
            select(Production).where(
                Production.clip_id.in_(clip_ids),
                Production.kind == "short",
            )
        )
    )
    production_by_id = {item.id: item for item in productions}
    production_ids = list(production_by_id)
    publications = (
        list(
            session.scalars(
                select(Publication).where(Publication.production_id.in_(production_ids))
            )
        )
        if production_ids
        else []
    )
    latest = _latest_snapshots(session, [item.id for item in publications])
    best_publication_by_clip: dict[
        uuid.UUID, tuple[Production, Publication, PublicationAnalyticsSnapshot]
    ] = {}
    for publication in publications:
        production = production_by_id.get(publication.production_id)
        snapshot = latest.get(publication.id)
        if production is None or snapshot is None:
            continue
        current = best_publication_by_clip.get(production.clip_id)
        current_popularity = (
            max(int(current[2].engaged_views or 0), int(current[2].views or 0))
            if current
            else -1
        )
        popularity = max(int(snapshot.engaged_views or 0), int(snapshot.views or 0))
        if popularity > current_popularity:
            best_publication_by_clip[production.clip_id] = (
                production,
                publication,
                snapshot,
            )

    signals: list[CandidateSignals] = []
    metadata_by_clip: dict[uuid.UUID, dict[str, object]] = {}
    for features, clip in rows:
        ai = _active_ai_features(features)
        evidence = best_publication_by_clip.get(clip.id)
        production = evidence[0] if evidence else None
        publication = evidence[1] if evidence else None
        snapshot = evidence[2] if evidence else None
        views = int(snapshot.views or 0) if snapshot else 0
        engaged_views = int(snapshot.engaged_views or 0) if snapshot else 0
        avp = float(snapshot.average_view_percentage or 0) if snapshot else 0.0
        shares = int(snapshot.shares or 0) if snapshot else 0
        comments = int(snapshot.comments or 0) if snapshot else 0
        subscribers = int(snapshot.subscribers_gained or 0) if snapshot else 0
        surprise = _number(ai.get("surprise_score"))
        humor = _number(ai.get("humor_score"))
        comment_potential = _number(ai.get("comment_potential"))
        categories = _text_list(ai.get("categories"))
        tone = _text_list(ai.get("tone"))

        signals.append(
            CandidateSignals(
                clip_id=str(clip.id),
                duration_seconds=float(clip.duration_seconds or 0),
                feature_score=float(features.candidate_score or 0),
                hook_score=_number(ai.get("hook_score")),
                payoff_score=max(humor, surprise),
                surprise_score=surprise,
                rewatch_score=_number(ai.get("rewatch_potential")),
                views=views,
                engaged_views=engaged_views,
                average_view_percentage=avp,
                shares=shares,
                comments=comments,
                subscribers_gained=subscribers,
                categories=categories,
                tone=tone,
                source_creator=creator_by_clip.get(clip.id),
                short_production_id=str(production.id) if production else None,
                short_publication_id=str(publication.id) if publication else None,
            )
        )
        metadata_by_clip[clip.id] = {
            "event_summary": str(ai.get("event_summary") or ""),
            "comment_potential": comment_potential,
            "analytics_sampled_at": (
                snapshot.sampled_at.isoformat() if snapshot and snapshot.sampled_at else None
            ),
            "measured_short": snapshot is not None,
            "score_breakdown": dict(features.score_breakdown or {}),
        }

    scored = score_pool(signals)
    evidence_pool: list[CandidateEvidence] = []
    for item in scored:
        clip_id = uuid.UUID(item.signals.clip_id)
        meta = metadata_by_clip[clip_id]
        evidence_pool.append(
            CandidateEvidence(
                clip_id=clip_id,
                deterministic_score=item.deterministic_score,
                opening_score=item.opening_score,
                duration_seconds=item.signals.duration_seconds,
                event_summary=str(meta["event_summary"]),
                categories=list(item.signals.categories),
                tone=list(item.signals.tone),
                source_creator=item.signals.source_creator,
                short_production_id=(
                    uuid.UUID(item.signals.short_production_id)
                    if item.signals.short_production_id
                    else None
                ),
                short_publication_id=(
                    uuid.UUID(item.signals.short_publication_id)
                    if item.signals.short_publication_id
                    else None
                ),
                views=item.signals.views,
                engaged_views=item.signals.engaged_views,
                average_view_percentage=item.signals.average_view_percentage,
                shares=item.signals.shares,
                comments=item.signals.comments,
                subscribers_gained=item.signals.subscribers_gained,
                feature_score=item.signals.feature_score,
                hook_score=item.signals.hook_score,
                payoff_score=item.signals.payoff_score,
                surprise_score=item.signals.surprise_score,
                comment_potential=float(meta["comment_potential"]),
                rewatch_score=item.signals.rewatch_score,
                evidence={
                    **meta,
                    "deterministic_breakdown": item.score_breakdown,
                },
            )
        )

    themed = [item for item in evidence_pool if _theme_match(theme, item)]
    eligible = themed if len(themed) >= settings.longform_min_segments else evidence_pool
    scored_by_id = {uuid.UUID(item.signals.clip_id): item for item in scored}
    scored_eligible = [scored_by_id[item.clip_id] for item in eligible]
    selected = sequence_pool(
        scored_eligible,
        target_duration_seconds=target_duration_seconds,
        min_segments=settings.longform_min_segments,
        max_segments=settings.longform_max_segments,
        target_segment_count=target_segment_count,
    )
    evidence_by_id = {item.clip_id: item for item in evidence_pool}
    return [evidence_by_id[uuid.UUID(item.signals.clip_id)] for item in selected]
