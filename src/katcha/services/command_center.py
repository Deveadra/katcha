from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from katcha.db import session_scope
from katcha.edit_performance_models import EditBlueprintPerformanceSnapshot
from katcha.intelligence_models import ChannelProfile
from katcha.longform_models import Compilation
from katcha.models import Clip, ClipAnalysisRun, ClipFeature, SourceItem
from katcha.packaging_intelligence_models import PackagingIntelligenceSnapshot
from katcha.production_models import Production, ProductionReview
from katcha.publishing_models import Publication
from katcha.render_models import RenderAttempt
from katcha.services.channel_editorial import score_clip_for_channel
from katcha.services.channel_profiles import ensure_active_profile

_FAILURE_STATES = {"failed", "dead_letter", "retry_exhausted", "error"}
_STOP_WORDS = {
    "best", "clip", "clips", "found", "today", "show", "me", "the", "and", "why",
    "they", "scored", "highly", "what", "currently", "failing", "make", "create",
    "episode", "from", "these", "five", "using", "commentary", "recipe", "look",
    "yesterday", "performance", "tell", "editing", "behavior", "should", "change",
    "katcha", "reject", "rejected", "this", "that", "please", "current",
}


def _profile(channel_profile_id: uuid.UUID) -> ChannelProfile:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        session.expunge(profile)
        return profile


def classify_intent(prompt: str, selected_clip_ids: list[uuid.UUID]) -> str:
    text = prompt.casefold()
    if any(word in text for word in ("failing", "failed", "failure", "broken", "error")):
        return "failures"
    if any(word in text for word in ("reject", "rejected", "rejection")):
        return "clip_rejection"
    if "performance" in text or ("editing" in text and "change" in text):
        return "performance_advice"
    if any(word in text for word in ("make", "create", "build")) and any(
        word in text for word in ("episode", "video", "production")
    ):
        return "create_content"
    if "clip" in text and any(word in text for word in ("best", "top", "strongest", "highest")):
        return "best_clips"
    if selected_clip_ids and any(word in text for word in ("score", "why", "explain")):
        return "clip_explanation"
    return "channel_status"


def _search_terms(prompt: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.-]{1,40}", prompt)
    values: list[str] = []
    for token in tokens:
        key = token.casefold()
        if key in _STOP_WORDS or key.isdigit() or len(key) < 3:
            continue
        if key not in values:
            values.append(key)
    return values[:6]


def _source_for_clip(session: object, clip_id: uuid.UUID) -> SourceItem | None:
    return session.scalar(
        select(SourceItem)
        .where(SourceItem.clip_id == clip_id)
        .order_by(SourceItem.discovered_at.desc())
        .limit(1)
    )


def _clip_haystack(clip: Clip, features: ClipFeature, source: SourceItem | None) -> str:
    return " ".join(
        [
            str(source.title if source else ""),
            str(source.creator if source else ""),
            str(source.source_metadata if source else ""),
            str(features.ai_features or {}),
            str(features.score_breakdown or {}),
            str(features.transcript or ""),
            str(clip.media_metadata or {}),
        ]
    ).casefold()


def _score_reasons(features: ClipFeature) -> list[str]:
    reasons: list[tuple[float, str]] = []
    for key, value in (features.score_breakdown or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        reasons.append((number, key.replace("_", " ")))
    ai = features.ai_features or {}
    active = ai.get("deep") if isinstance(ai.get("deep"), dict) else ai.get("bulk")
    if isinstance(active, dict):
        for key in (
            "hook_score",
            "surprise_score",
            "humor_score",
            "comment_potential",
            "rewatch_potential",
        ):
            try:
                value = float(active.get(key))
            except (TypeError, ValueError):
                continue
            reasons.append((value / 100.0, key.replace("_", " ")))
    return [label for _, label in sorted(reasons, reverse=True)[:3]]


def best_clips(
    channel_profile_id: uuid.UUID,
    prompt: str,
    *,
    limit: int = 5,
) -> tuple[str, list[dict[str, object]]]:
    profile = _profile(channel_profile_id)
    try:
        zone = ZoneInfo(profile.timezone)
    except Exception:
        zone = ZoneInfo("UTC")
    local_now = datetime.now(UTC).astimezone(zone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_midnight.astimezone(UTC)
    terms = _search_terms(prompt)

    candidates: list[tuple[float, Clip, ClipFeature, SourceItem | None, dict[str, object]]] = []
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Clip, ClipFeature)
                .join(ClipFeature, ClipFeature.clip_id == Clip.id)
                .where(
                    Clip.created_at >= start_utc,
                    ClipFeature.candidate_score.is_not(None),
                )
                .order_by(desc(ClipFeature.candidate_score))
                .limit(75)
            )
        )
        for clip, features in rows:
            source = _source_for_clip(session, clip.id)
            haystack = _clip_haystack(clip, features, source)
            if terms and not all(term in haystack for term in terms):
                continue
            try:
                learned = score_clip_for_channel(channel_profile_id, clip.id)
                learned_score = float(learned["score"])
            except ValueError:
                learned = {}
                learned_score = float(features.candidate_score or 0) / 100.0
            candidates.append((learned_score, clip, features, source, learned))

    candidates.sort(key=lambda row: row[0], reverse=True)
    selected = candidates[:limit]
    evidence: list[dict[str, object]] = []
    for rank, (learned_score, clip, features, source, learned) in enumerate(selected, start=1):
        reasons = _score_reasons(features)
        evidence.append(
            {
                "kind": "clip",
                "id": str(clip.id),
                "rank": rank,
                "title": source.title if source and source.title else f"Clip {str(clip.id)[:8]}",
                "platform": source.platform if source else None,
                "creator": source.creator if source else None,
                "candidate_score": float(features.candidate_score or 0),
                "channel_score": round(learned_score, 4),
                "why": reasons,
                "source_url": source.canonical_url if source else None,
                "created_at": clip.created_at.isoformat(),
                "details": learned.get("details") if learned else {},
            }
        )
    if not evidence:
        topic = ", ".join(terms) if terms else "the requested topic"
        return (
            "I do not have any scored clips from today matching "
            f"{topic} in this channel's stored data.",
            [],
        )
    topic = ", ".join(terms) if terms else "today's inventory"
    lead = evidence[0]
    return (
        f"I found {len(evidence)} strong clip{'s' if len(evidence) != 1 else ''} for {topic}. "
        f"The highest-ranked item is {lead['title']} with a channel score of "
        f"{float(lead['channel_score']) * 100:.1f}/100. "
        "The evidence cards show the stored score drivers; I have not inferred missing metrics.",
        evidence,
    )


def failures(channel_profile_id: uuid.UUID) -> tuple[str, list[dict[str, object]]]:
    evidence: list[dict[str, object]] = []
    with session_scope() as session:
        productions = list(
            session.scalars(
                select(Production)
                .where(
                    Production.channel_profile_id == channel_profile_id,
                    Production.status.in_(_FAILURE_STATES),
                )
                .order_by(Production.updated_at.desc())
                .limit(20)
            )
        )
        compilations = list(
            session.scalars(
                select(Compilation)
                .where(
                    Compilation.channel_profile_id == channel_profile_id,
                    Compilation.status.in_(_FAILURE_STATES),
                )
                .order_by(Compilation.updated_at.desc())
                .limit(20)
            )
        )
        render_attempts = list(
            session.scalars(
                select(RenderAttempt)
                .where(
                    RenderAttempt.channel_profile_id == channel_profile_id,
                    RenderAttempt.status.in_(_FAILURE_STATES),
                )
                .order_by(RenderAttempt.updated_at.desc())
                .limit(20)
            )
        )
        publications = list(
            session.scalars(
                select(Publication)
                .join(
                    ChannelProfile,
                    Publication.youtube_connection_id
                    == ChannelProfile.youtube_connection_id,
                )
                .where(
                    ChannelProfile.id == channel_profile_id,
                    Publication.status.in_(_FAILURE_STATES),
                )
                .order_by(Publication.updated_at.desc())
                .limit(20)
            )
        )

    seen: set[tuple[str, str]] = set()
    for kind, rows in (
        ("production", productions),
        ("compilation", compilations),
        ("render_attempt", render_attempts),
        ("publication", publications),
    ):
        for row in rows:
            key = (kind, str(row.id))
            if key in seen:
                continue
            seen.add(key)
            error = getattr(row, "error", None) or getattr(row, "failure_reason", None)
            evidence.append(
                {
                    "kind": kind,
                    "id": str(row.id),
                    "status": str(getattr(row, "status", "unknown")),
                    "stage": str(getattr(row, "stage", "unknown")),
                    "error": str(error) if error else None,
                    "updated_at": getattr(row, "updated_at", None),
                    "production_id": (
                        str(row.production_id)
                        if kind == "render_attempt" and row.production_id
                        else None
                    ),
                }
            )
    evidence.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    if not evidence:
        return (
            "I do not see a current failed production, compilation, render "
            "attempt, or publication for this channel.",
            [],
        )
    return (
        f"I found {len(evidence)} current failure record{'s' if len(evidence) != 1 else ''}. "
        "I sorted the newest failures first and preserved the stored stage/error "
        "text so recovery decisions are auditable.",
        evidence[:30],
    )


def clip_explanation(
    channel_profile_id: uuid.UUID,
    clip_id: uuid.UUID,
) -> tuple[str, list[dict[str, object]]]:
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        clip = session.get(Clip, clip_id)
        features = session.get(ClipFeature, clip_id)
        source = _source_for_clip(session, clip_id) if clip else None
        analysis = session.scalar(
            select(ClipAnalysisRun)
            .where(ClipAnalysisRun.clip_id == clip_id)
            .order_by(ClipAnalysisRun.created_at.desc())
            .limit(1)
        )
        if clip is None:
            raise ValueError(f"clip not found: {clip_id}")
        reviews = list(
            session.scalars(
                select(ProductionReview)
                .join(Production, ProductionReview.production_id == Production.id)
                .where(
                    Production.channel_profile_id == channel_profile_id,
                    Production.clip_id == clip_id,
                )
                .order_by(ProductionReview.created_at.desc())
                .limit(10)
            )
        )
        payload = {
            "kind": "clip",
            "id": str(clip.id),
            "title": source.title if source and source.title else f"Clip {str(clip.id)[:8]}",
            "clip_status": clip.status,
            "candidate_score": (
                float(features.candidate_score)
                if features and features.candidate_score is not None
                else None
            ),
            "score_breakdown": dict(features.score_breakdown or {}) if features else {},
            "ai_features": dict(features.ai_features or {}) if features else {},
            "latest_analysis_status": analysis.status if analysis else None,
            "latest_analysis_stage": analysis.stage if analysis else None,
            "latest_analysis_error": analysis.error if analysis else None,
            "latest_escalation_reason": analysis.escalation_reason if analysis else None,
            "reviews": [
                {
                    "decision": row.decision,
                    "note": row.note,
                    "created_at": row.created_at.isoformat(),
                }
                for row in reviews
            ],
        }
    reason_bits: list[str] = []
    if payload["latest_analysis_error"]:
        reason_bits.append(f"analysis error: {payload['latest_analysis_error']}")
    if payload["reviews"]:
        last = payload["reviews"][0]
        review_note = f" — {last['note']}" if last["note"] else ""
        reason_bits.append(f"latest review: {last['decision']}{review_note}")
    if payload["candidate_score"] is not None:
        reason_bits.append(f"candidate score {payload['candidate_score']:.1f}/100")
    if not reason_bits:
        reason_bits.append("no explicit rejection reason is stored")
    return (
        f"For {payload['title']}, " + "; ".join(reason_bits) + ". "
        "I am separating stored rejection/review evidence from my explanation "
        "rather than inventing a motive.",
        [payload],
    )


def performance_advice(channel_profile_id: uuid.UUID) -> tuple[str, list[dict[str, object]]]:
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        edit = session.scalar(
            select(EditBlueprintPerformanceSnapshot)
            .where(EditBlueprintPerformanceSnapshot.channel_profile_id == channel_profile_id)
            .order_by(EditBlueprintPerformanceSnapshot.created_at.desc())
            .limit(1)
        )
        packaging = session.scalar(
            select(PackagingIntelligenceSnapshot)
            .where(PackagingIntelligenceSnapshot.channel_profile_id == channel_profile_id)
            .order_by(PackagingIntelligenceSnapshot.created_at.desc())
            .limit(1)
        )
    evidence: list[dict[str, object]] = []
    if edit is not None:
        evidence.append(
            {
                "kind": "edit_performance",
                "id": str(edit.id),
                "age_bucket_hours": edit.age_bucket_hours,
                "publication_count": edit.publication_count,
                "aggregate_metrics": list(edit.aggregate_metrics or []),
                "comparison_status": edit.comparison_status,
                "comparison_summary": dict(edit.comparison_summary or {}),
                "sample_window_start": edit.sample_window_start,
                "sample_window_end": edit.sample_window_end,
            }
        )
    if packaging is not None:
        evidence.append(
            {
                "kind": "packaging_intelligence",
                "id": str(packaging.id),
                "publication_count": packaging.publication_count,
                "recommendation_status": packaging.recommendation_status,
                "recommendations": list(packaging.recommendations or []),
                "sample_window_start": packaging.sample_window_start,
                "sample_window_end": packaging.sample_window_end,
            }
        )
    if not evidence:
        return (
            "Katcha does not have enough stored performance evidence yet to "
            "recommend an editing behavior change for this channel.",
            [],
        )
    if edit is not None and edit.comparison_status != "ready":
        return (
            f"Katcha has {edit.publication_count} maturity-matched publication "
            "samples, but the edit comparison is still "
            f"{edit.comparison_status.replace('_', ' ')}. I can summarize the "
            "evidence, but I would not change a channel recipe from weak "
            "evidence yet.",
            evidence,
        )
    return (
        "Katcha has channel-scoped edit and/or packaging evidence ready for review. "
        "I will base any recommendation on the measured aggregates below, "
        "not on generic YouTube advice.",
        evidence,
    )


def channel_status(channel_profile_id: uuid.UUID) -> tuple[str, list[dict[str, object]]]:
    profile = _profile(channel_profile_id)
    with session_scope() as session:
        recent_productions = list(
            session.scalars(
                select(Production)
                .where(Production.channel_profile_id == channel_profile_id)
                .order_by(Production.updated_at.desc())
                .limit(8)
            )
        )
    evidence = [
        {
            "kind": "channel",
            "id": str(profile.id),
            "status": profile.status,
            "timezone": profile.timezone,
            "recent_productions": [
                {
                    "id": str(row.id),
                    "status": row.status,
                    "stage": row.stage,
                    "clip_id": str(row.clip_id),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in recent_productions
            ],
        }
    ]
    return (
        f"This channel is {profile.status}. I included the most recent "
        "production states so the answer is tied to current stored Katcha state.",
        evidence,
    )
