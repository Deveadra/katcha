from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select

from katcha.command_center_models import CommandTurn
from katcha.db import session_scope
from katcha.edit_performance_models import EditBlueprintPerformanceSnapshot
from katcha.editorial.rankings import get_ranking_format
from katcha.intelligence_models import ChannelProfile, PerformanceObservation
from katcha.longform_models import Compilation
from katcha.models import Clip, ClipAnalysisRun, ClipFeature, SourceItem
from katcha.packaging_intelligence_models import PackagingIntelligenceSnapshot
from katcha.production_models import Production, ProductionReview
from katcha.publishing_models import Publication
from katcha.render_models import RenderAttempt
from katcha.services.channel_brands import brand_for_channel
from katcha.services.channel_editorial import score_clip_for_channel
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.short_episodes import ShortEpisodeCandidateInput
from katcha.short_episode_models import ShortEpisode

_FAILURE_STATES = {"failed", "dead_letter", "retry_exhausted", "error"}
_STOP_WORDS = {
    "best",
    "clip",
    "clips",
    "found",
    "today",
    "show",
    "me",
    "the",
    "and",
    "why",
    "they",
    "scored",
    "highly",
    "what",
    "currently",
    "failing",
    "make",
    "create",
    "episode",
    "from",
    "these",
    "five",
    "using",
    "commentary",
    "recipe",
    "look",
    "yesterday",
    "performance",
    "tell",
    "editing",
    "behavior",
    "should",
    "change",
    "katcha",
    "reject",
    "rejected",
    "this",
    "that",
    "please",
    "current",
    "latest",
    "explain",
    "score",
    "scores",
    "scoring",
    "rank",
    "ranked",
    "ranking",
    "strong",
    "strongest",
    "high",
    "were",
    "was",
    "are",
    "is",
    "about",
    "how",
    "same",
    "thing",
    "do",
    "into",
    "one",
}


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True, slots=True)
class CommandFollowUpResolution:
    effective_prompt: str
    selected_clip_ids: tuple[uuid.UUID, ...]
    intent_hint: str | None = None
    inherited_from_thread: bool = False
    source_turn_id: uuid.UUID | None = None
    resolution: str | None = None
    action_source_turn_id: uuid.UUID | None = None


_ORDINAL_INDEX = {
    "first": 0,
    "1st": 0,
    "top": 0,
    "second": 1,
    "2nd": 1,
    "third": 2,
    "3rd": 2,
    "fourth": 3,
    "4th": 3,
    "fifth": 4,
    "5th": 4,
    "sixth": 5,
    "6th": 5,
    "seventh": 6,
    "7th": 6,
    "eighth": 7,
    "8th": 7,
    "ninth": 8,
    "9th": 8,
    "tenth": 9,
    "10th": 9,
}


def _turn_clip_ids(turn: CommandTurn) -> tuple[uuid.UUID, ...]:
    values: list[uuid.UUID] = []
    for item in list(turn.evidence or []):
        if item.get("kind") != "clip" or not item.get("id"):
            continue
        try:
            value = uuid.UUID(str(item["id"]))
        except (TypeError, ValueError):
            continue
        if value not in values:
            values.append(value)
    return tuple(values)


def _context_selected_clip_ids(turn: CommandTurn | None) -> tuple[uuid.UUID, ...]:
    if turn is None:
        return ()
    values: list[uuid.UUID] = []
    for raw in list((turn.turn_context or {}).get("resolved_selected_clip_ids") or []):
        try:
            value = uuid.UUID(str(raw))
        except (TypeError, ValueError):
            continue
        if value not in values:
            values.append(value)
    if values:
        return tuple(values)
    for raw in list((turn.turn_context or {}).get("selected_clip_ids") or []):
        try:
            value = uuid.UUID(str(raw))
        except (TypeError, ValueError):
            continue
        if value not in values:
            values.append(value)
    return tuple(values)


def _latest_conversation_reference(
    turns: list[CommandTurn],
) -> tuple[CommandTurn | None, CommandTurn | None]:
    assistant = next(
        (turn for turn in reversed(turns) if turn.role == "assistant"),
        None,
    )
    if assistant is None:
        return None, None
    user = next(
        (
            turn
            for turn in reversed(turns)
            if turn.role == "user"
            and turn.sequence_number < assistant.sequence_number
            and (
                assistant.request_id is None
                or turn.request_id == assistant.request_id
            )
        ),
        None,
    )
    return assistant, user


def _ordinal_reference(text: str) -> int | None:
    for label, index in _ORDINAL_INDEX.items():
        if re.search(rf"\b{re.escape(label)}\b", text):
            return index
    if re.search(r"\blast\s+(?:one|clip|item)\b", text):
        return -1
    return None


def _looks_like_follow_up(text: str) -> bool:
    normalized = " ".join(text.strip().casefold().split())
    return (
        normalized.startswith(("what about ", "how about ", "and "))
        or normalized
        in {
            "why",
            "why?",
            "explain",
            "explain that",
            "same",
            "same thing",
            "do the same",
            "what about yesterday?",
            "what about today?",
        }
    )


def _looks_like_confirmation(text: str) -> bool:
    normalized = " ".join(
        re.sub(r"[^a-z0-9]+", " ", text.casefold()).split()
    )
    return normalized in {
        "yes",
        "yes do it",
        "do it",
        "go ahead",
        "confirm",
        "confirmed",
        "run it",
        "start it",
        "execute it",
        "sounds good",
        "looks good",
        "approved",
        "approve it",
    }


def resolve_command_follow_up(
    prompt: str,
    selected_clip_ids: list[uuid.UUID],
    turns: list[CommandTurn],
    *,
    has_pending_proposal: bool = False,
) -> CommandFollowUpResolution:
    if selected_clip_ids:
        return CommandFollowUpResolution(
            effective_prompt=prompt,
            selected_clip_ids=tuple(selected_clip_ids),
        )

    assistant, user = _latest_conversation_reference(turns)
    if assistant is None:
        return CommandFollowUpResolution(
            effective_prompt=prompt,
            selected_clip_ids=(),
        )

    text = prompt.casefold()
    evidence_ids = _turn_clip_ids(assistant)
    previous_selected = _context_selected_clip_ids(user)
    reference_ids = previous_selected or evidence_ids

    if has_pending_proposal and _looks_like_confirmation(prompt):
        return CommandFollowUpResolution(
            effective_prompt=prompt,
            selected_clip_ids=reference_ids,
            intent_hint="confirm_action",
            inherited_from_thread=True,
            source_turn_id=assistant.id,
            resolution=(
                "Chat text never confirms an executable proposal; the prior "
                "server-issued proposal must be reviewed and confirmed explicitly."
            ),
            action_source_turn_id=assistant.id,
        )

    ordinal = _ordinal_reference(text)
    if ordinal is not None and evidence_ids:
        index = len(evidence_ids) - 1 if ordinal == -1 else ordinal
        if 0 <= index < len(evidence_ids):
            return CommandFollowUpResolution(
                effective_prompt=prompt,
                selected_clip_ids=(evidence_ids[index],),
                inherited_from_thread=True,
                source_turn_id=assistant.id,
                resolution=(
                    f"Resolved the referenced item to stored clip rank {index + 1} "
                    "from the previous grounded answer."
                ),
            )

    create_requested = any(
        word in text for word in ("make", "create", "build", "turn")
    ) and any(
        word in text for word in ("episode", "video", "production", "short")
    )
    explain_requested = any(
        word in text for word in ("why", "explain", "score", "scored")
    )
    plural_reference = bool(
        re.search(r"\b(these|those|them|all of them|all those)\b", text)
    )
    singular_reference = bool(
        re.search(r"\b(this|that|it|one|that one|this one)\b", text)
    )

    if reference_ids and create_requested and plural_reference:
        return CommandFollowUpResolution(
            effective_prompt=prompt,
            selected_clip_ids=reference_ids,
            inherited_from_thread=True,
            source_turn_id=assistant.id,
            resolution=(
                "Resolved the plural reference to the ordered clip context from "
                "the previous grounded answer."
            ),
        )

    if reference_ids and (
        (create_requested and singular_reference)
        or (explain_requested and (singular_reference or len(prompt.split()) <= 4))
    ):
        return CommandFollowUpResolution(
            effective_prompt=prompt,
            selected_clip_ids=(reference_ids[0],),
            inherited_from_thread=True,
            source_turn_id=assistant.id,
            resolution=(
                "Resolved the singular reference to the first applicable clip "
                "from the previous grounded answer."
            ),
        )

    prior_intent = assistant.intent
    if prior_intent in {"best_clips", "performance_advice"} and _looks_like_follow_up(
        prompt
    ):
        effective_prompt = prompt
        if prior_intent == "best_clips" and user is not None:
            current_terms = _search_terms(prompt)
            if not current_terms:
                inherited_terms = _search_terms(user.content)
                if inherited_terms:
                    effective_prompt = " ".join([*inherited_terms, prompt])
        return CommandFollowUpResolution(
            effective_prompt=effective_prompt,
            selected_clip_ids=(),
            intent_hint=prior_intent,
            inherited_from_thread=True,
            source_turn_id=assistant.id,
            resolution=(
                f"Inherited the previous {prior_intent.replace('_', ' ')} "
                "question type for this follow-up."
            ),
        )

    return CommandFollowUpResolution(
        effective_prompt=prompt,
        selected_clip_ids=(),
    )


def _profile(channel_profile_id: uuid.UUID) -> ChannelProfile:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        session.expunge(profile)
        return profile


def _zone(profile: ChannelProfile) -> ZoneInfo:
    try:
        return ZoneInfo(profile.timezone)
    except Exception:
        return ZoneInfo("UTC")


def resolve_time_window(
    prompt: str,
    timezone: str,
    *,
    default_today: bool = False,
) -> TimeWindow | None:
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        zone = ZoneInfo("UTC")
    now_local = datetime.now(UTC).astimezone(zone)
    text = prompt.casefold()

    if "yesterday" in text:
        end_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = end_local - timedelta(days=1)
        label = f"{start_local.strftime('%b')} {start_local.day}"
    elif "today" in text:
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        label = "today"
    elif match := re.search(r"last\s+(\d{1,3})\s+hours?", text):
        hours = max(1, min(720, int(match.group(1))))
        end_local = now_local
        start_local = end_local - timedelta(hours=hours)
        label = f"the last {hours} hours"
    elif match := re.search(r"last\s+(\d{1,2})\s+days?", text):
        days = max(1, min(30, int(match.group(1))))
        end_local = now_local
        start_local = end_local - timedelta(days=days)
        label = f"the last {days} days"
    elif "this week" in text:
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = midnight - timedelta(days=midnight.weekday())
        end_local = now_local
        label = "this week"
    elif default_today:
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        label = "today"
    else:
        return None

    return TimeWindow(
        start=start_local.astimezone(UTC),
        end=end_local.astimezone(UTC),
        label=label,
    )


def classify_intent(prompt: str, selected_clip_ids: list[uuid.UUID]) -> str:
    text = prompt.casefold()
    if any(word in text for word in ("failing", "failed", "failure", "broken", "error")):
        return "failures"
    if any(word in text for word in ("reject", "rejected", "rejection")):
        return "clip_rejection"
    if "performance" in text or ("editing" in text and "change" in text):
        return "performance_advice"
    if any(word in text for word in ("make", "create", "build", "turn")) and any(
        word in text for word in ("episode", "video", "production", "short")
    ):
        return "create_content"
    if "clip" in text and any(
        word in text for word in ("best", "top", "strongest", "highest")
    ):
        return "best_clips"
    if selected_clip_ids and any(word in text for word in ("score", "why", "explain")):
        return "clip_explanation"
    return "channel_status"


def infer_edit_blueprint_key(prompt: str) -> str | None:
    text = prompt.casefold()
    if "commentary" in text:
        return "persona_commentary"
    if "header" in text or "explainer" in text:
        return "header_explainer"
    return None


def _search_terms(prompt: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.-]{1,40}", prompt)
    values: list[str] = []
    for token in tokens:
        key = token.casefold().rstrip(".-")
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


def _active_ai(features: ClipFeature) -> dict[str, object]:
    ai = dict(features.ai_features or {})
    if isinstance(ai.get("deep"), dict):
        return dict(ai["deep"])
    if isinstance(ai.get("bulk"), dict):
        return dict(ai["bulk"])
    return ai


def _number(payload: dict[str, object], key: str, fallback: float = 0.0) -> float:
    try:
        return max(0.0, min(100.0, float(payload.get(key, fallback) or fallback)))
    except (TypeError, ValueError):
        return fallback


def _score_reasons(features: ClipFeature) -> list[str]:
    reasons: list[tuple[float, str]] = []
    for key, value in (features.score_breakdown or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        reasons.append((number, key.replace("_", " ")))
    active = _active_ai(features)
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


def ranked_episode_allowed_counts(
    channel_profile_id: uuid.UUID,
) -> tuple[int, ...]:
    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        brand, _ = brand_for_channel(session, profile.id)
        reference = brand.editorial_format
        if reference is None:
            return ()
        try:
            contract = get_ranking_format(reference.key, reference.version)
        except KeyError:
            return ()
        return tuple(contract.allowed_item_counts)


def build_short_episode_candidates(
    clip_ids: list[uuid.UUID],
) -> list[ShortEpisodeCandidateInput]:
    if not clip_ids:
        raise ValueError("ranked episode requires selected clips")
    if len(clip_ids) != len(set(clip_ids)):
        raise ValueError("selected clips must be unique")

    candidates: list[ShortEpisodeCandidateInput] = []
    with session_scope() as session:
        for clip_id in clip_ids:
            clip = session.get(Clip, clip_id)
            features = session.get(ClipFeature, clip_id)
            if clip is None:
                raise ValueError(f"clip not found: {clip_id}")
            if features is None or features.candidate_score is None:
                raise ValueError(f"clip must have completed scoring: {clip_id}")
            active = _active_ai(features)
            feature_score = float(features.candidate_score or 0)
            surprise = _number(active, "surprise_score")
            humor = _number(active, "humor_score")
            candidates.append(
                ShortEpisodeCandidateInput(
                    clip_id=clip_id,
                    hook_strength=_number(active, "hook_score", feature_score),
                    visual_clarity=_number(
                        active,
                        "visual_clarity",
                        feature_score,
                    ),
                    payoff_strength=max(
                        _number(active, "payoff_score"),
                        humor,
                        surprise,
                    ),
                    escalation_value=_number(
                        active,
                        "escalation_value",
                        surprise,
                    ),
                    commentary_opportunity=_number(
                        active,
                        "comment_potential",
                        feature_score,
                    ),
                    novelty=_number(active, "novelty", surprise),
                    source_quality=_number(
                        active,
                        "source_quality",
                        feature_score,
                    ),
                )
            )
    return candidates


def best_clips(
    channel_profile_id: uuid.UUID,
    prompt: str,
    *,
    limit: int = 5,
) -> tuple[str, list[dict[str, object]]]:
    profile = _profile(channel_profile_id)
    window = resolve_time_window(prompt, profile.timezone, default_today=True)
    assert window is not None
    terms = _search_terms(prompt)

    candidates: list[
        tuple[float, Clip, ClipFeature, SourceItem, dict[str, object]]
    ] = []
    seen: set[uuid.UUID] = set()
    with session_scope() as session:
        rows = list(
            session.execute(
                select(SourceItem, Clip, ClipFeature)
                .join(Clip, Clip.id == SourceItem.clip_id)
                .join(ClipFeature, ClipFeature.clip_id == Clip.id)
                .where(
                    SourceItem.discovered_at >= window.start,
                    SourceItem.discovered_at < window.end,
                    ClipFeature.candidate_score.is_not(None),
                )
                .order_by(
                    desc(ClipFeature.candidate_score),
                    desc(SourceItem.discovered_at),
                )
                .limit(250)
            )
        )
        for source, clip, features in rows:
            if clip.id in seen:
                continue
            seen.add(clip.id)
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
    for rank, (learned_score, clip, features, source, learned) in enumerate(
        selected,
        start=1,
    ):
        evidence.append(
            {
                "kind": "clip",
                "id": str(clip.id),
                "rank": rank,
                "title": source.title or f"Clip {str(clip.id)[:8]}",
                "platform": source.platform,
                "creator": source.creator,
                "candidate_score": float(features.candidate_score or 0),
                "channel_score": round(learned_score, 4),
                "why": _score_reasons(features),
                "source_url": source.canonical_url,
                "discovered_at": source.discovered_at.isoformat(),
                "time_window": {
                    "label": window.label,
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                },
                "details": learned.get("details") if learned else {},
            }
        )
    topic = ", ".join(terms) if terms else "the requested topic"
    if not evidence:
        return (
            f"I do not have any scored clips discovered in {window.label} "
            f"matching {topic} in this channel's stored data.",
            [],
        )
    lead = evidence[0]
    return (
        f"I found {len(evidence)} strong clip{'s' if len(evidence) != 1 else ''} "
        f"for {topic} in {window.label}. The highest-ranked item is "
        f"{lead['title']} with a channel score of "
        f"{float(lead['channel_score']) * 100:.1f}/100. "
        "The evidence cards show the stored score drivers and discovery times.",
        evidence,
    )


def _lineage_root(
    row: Production | Compilation | ShortEpisode,
    by_id: dict[uuid.UUID, Production | Compilation | ShortEpisode],
    parent_field: str,
) -> uuid.UUID:
    current = row
    visited: set[uuid.UUID] = set()
    while True:
        parent_id = getattr(current, parent_field)
        if parent_id is None or parent_id in visited:
            return current.id
        visited.add(current.id)
        parent = by_id.get(parent_id)
        if parent is None:
            return parent_id
        current = parent


def _latest_lineage_rows(
    rows: list[Production] | list[Compilation] | list[ShortEpisode],
    parent_field: str,
) -> list[Production | Compilation | ShortEpisode]:
    by_id = {row.id: row for row in rows}
    latest: dict[uuid.UUID, Production | Compilation | ShortEpisode] = {}
    for row in rows:
        root = _lineage_root(row, by_id, parent_field)
        existing = latest.get(root)
        if existing is None or (
            int(getattr(row, "generation", 1)),
            row.updated_at or row.created_at,
        ) > (
            int(getattr(existing, "generation", 1)),
            existing.updated_at or existing.created_at,
        ):
            latest[root] = row
    return list(latest.values())


def failures(channel_profile_id: uuid.UUID) -> tuple[str, list[dict[str, object]]]:
    evidence: list[dict[str, object]] = []
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        production_rows = list(
            session.scalars(
                select(Production).where(
                    Production.channel_profile_id == channel_profile_id
                )
            )
        )
        compilation_rows = list(
            session.scalars(
                select(Compilation).where(
                    Compilation.channel_profile_id == channel_profile_id
                )
            )
        )
        episode_rows = list(
            session.scalars(
                select(ShortEpisode).where(
                    ShortEpisode.channel_profile_id == channel_profile_id
                )
            )
        )
        productions = _latest_lineage_rows(
            production_rows,
            "parent_production_id",
        )
        compilations = _latest_lineage_rows(
            compilation_rows,
            "parent_compilation_id",
        )
        episodes = _latest_lineage_rows(
            episode_rows,
            "parent_episode_id",
        )

        current_production_ids = {row.id for row in productions}
        current_episode_ids = {row.id for row in episodes}
        render_rows = list(
            session.scalars(
                select(RenderAttempt)
                .where(RenderAttempt.channel_profile_id == channel_profile_id)
                .order_by(RenderAttempt.updated_at.asc())
            )
        )
        latest_render: dict[tuple[str, uuid.UUID], RenderAttempt] = {}
        for attempt in render_rows:
            if attempt.production_id in current_production_ids:
                latest_render[("production", attempt.production_id)] = attempt
            if attempt.short_episode_id in current_episode_ids:
                latest_render[("short_episode", attempt.short_episode_id)] = attempt

        current_compilation_ids = {row.id for row in compilations}
        publications = [
            row
            for row in session.scalars(
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
                .limit(100)
            )
            if (
                row.production_id in current_production_ids
                or row.short_episode_id in current_episode_ids
                or row.compilation_id in current_compilation_ids
            )
        ]

    def add_source(
        kind: str,
        row: Production | Compilation | ShortEpisode,
    ) -> None:
        render = latest_render.get((kind, row.id))
        if render is not None and render.status in _FAILURE_STATES:
            evidence.append(
                {
                    "kind": "render_attempt",
                    "id": str(render.id),
                    "status": render.status,
                    "stage": render.stage,
                    "error": render.error,
                    "updated_at": render.updated_at,
                    "source_type": kind,
                    "production_id": (
                        str(row.id) if kind == "production" else None
                    ),
                    "short_episode_id": (
                        str(row.id) if kind == "short_episode" else None
                    ),
                    "source_generation": render.source_generation,
                    "attempt_number": render.attempt_number,
                }
            )
            return
        if row.status in _FAILURE_STATES:
            evidence.append(
                {
                    "kind": kind,
                    "id": str(row.id),
                    "status": row.status,
                    "stage": row.stage,
                    "error": getattr(row, "error", None),
                    "updated_at": row.updated_at,
                    "generation": getattr(row, "generation", 1),
                }
            )

    for row in productions:
        add_source("production", row)
    for row in episodes:
        add_source("short_episode", row)
    for row in compilations:
        add_source("compilation", row)
    for row in publications:
        evidence.append(
            {
                "kind": "publication",
                "id": str(row.id),
                "status": row.status,
                "stage": row.stage,
                "error": row.error or row.failure_reason,
                "updated_at": row.updated_at,
            }
        )

    evidence.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    if not evidence:
        return (
            "I do not see a currently unresolved production, ranked episode, "
            "compilation, render, or publication failure for this channel.",
            [],
        )
    return (
        f"I found {len(evidence)} unresolved failure "
        f"record{'s' if len(evidence) != 1 else ''}. Older failed generations "
        "that have a newer recovery generation are excluded.",
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
            "title": (
                source.title
                if source and source.title
                else f"Clip {str(clip.id)[:8]}"
            ),
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


def _windowed_performance(
    channel_profile_id: uuid.UUID,
    window: TimeWindow,
) -> tuple[str, list[dict[str, object]]]:
    with session_scope() as session:
        observations = list(
            session.scalars(
                select(PerformanceObservation)
                .where(
                    PerformanceObservation.channel_profile_id
                    == channel_profile_id,
                    PerformanceObservation.observed_at >= window.start,
                    PerformanceObservation.observed_at < window.end,
                )
                .order_by(PerformanceObservation.observed_at.asc())
            )
        )
        latest_by_publication: dict[uuid.UUID, PerformanceObservation] = {}
        for observation in observations:
            latest_by_publication[observation.publication_id] = observation
        rows = list(latest_by_publication.values())

        production_ids = {
            row.source_id for row in rows if row.source_kind == "production"
        }
        episode_ids = {
            row.source_id for row in rows if row.source_kind == "short_episode"
        }
        productions = {
            item.id: item
            for item in session.scalars(
                select(Production).where(Production.id.in_(production_ids))
            )
        } if production_ids else {}
        episodes = {
            item.id: item
            for item in session.scalars(
                select(ShortEpisode).where(ShortEpisode.id.in_(episode_ids))
            )
        } if episode_ids else {}

    groups: dict[str, dict[str, float | int | str]] = {}
    for row in rows:
        if row.source_kind == "production":
            source = productions.get(row.source_id)
            key = source.edit_blueprint_key if source else None
        elif row.source_kind == "short_episode":
            source = episodes.get(row.source_id)
            key = source.edit_blueprint_key if source else None
        else:
            key = "longform_compilation"
        group_key = key or "unversioned"
        group = groups.setdefault(
            group_key,
            {
                "edit_blueprint_key": group_key,
                "sample_count": 0,
                "outcome_total": 0.0,
                "average_view_total": 0.0,
                "views": 0,
                "subscribers_gained": 0,
            },
        )
        labels = dict(row.labels or {})
        group["sample_count"] = int(group["sample_count"]) + 1
        group["outcome_total"] = float(group["outcome_total"]) + float(
            row.outcome_score
        )
        group["average_view_total"] = float(
            group["average_view_total"]
        ) + float(labels.get("average_view_percentage") or 0)
        group["views"] = int(group["views"]) + int(labels.get("views") or 0)
        group["subscribers_gained"] = int(group["subscribers_gained"]) + int(
            labels.get("subscribers_gained") or 0
        )

    summaries: list[dict[str, object]] = []
    for group in groups.values():
        count = int(group["sample_count"])
        summaries.append(
            {
                "edit_blueprint_key": group["edit_blueprint_key"],
                "sample_count": count,
                "mean_outcome_score": round(
                    float(group["outcome_total"]) / count,
                    4,
                ),
                "mean_average_view_percentage": round(
                    float(group["average_view_total"]) / count,
                    2,
                ),
                "views": int(group["views"]),
                "subscribers_gained": int(group["subscribers_gained"]),
            }
        )
    summaries.sort(
        key=lambda item: float(item["mean_outcome_score"]),
        reverse=True,
    )
    evidence = [
        {
            "kind": "windowed_edit_performance",
            "id": f"{window.start.isoformat()}:{window.end.isoformat()}",
            "window_label": window.label,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "sample_count": len(rows),
            "groups": summaries,
        }
    ] if rows else []
    if not rows:
        return (
            f"Katcha has no performance observations stored for {window.label}, "
            "so I cannot honestly recommend an editing behavior change from that period.",
            [],
        )
    if len(summaries) < 2 or any(
        int(item["sample_count"]) < 2 for item in summaries[:2]
    ):
        return (
            f"Katcha has {len(rows)} measured publication sample"
            f"{'s' if len(rows) != 1 else ''} from {window.label}, but not enough "
            "repeat evidence across editing recipes to recommend changing behavior yet.",
            evidence,
        )
    leader, runner_up = summaries[:2]
    delta = (
        float(leader["mean_outcome_score"])
        - float(runner_up["mean_outcome_score"])
    )
    return (
        f"For {window.label}, {leader['edit_blueprint_key']} led the next measured "
        f"recipe by {delta:.3f} outcome-score points across the stored samples. "
        "That is evidence for a controlled follow-up test, not a reason to rewrite "
        "the channel recipe from one day alone.",
        evidence,
    )


def performance_advice(
    channel_profile_id: uuid.UUID,
    prompt: str,
) -> tuple[str, list[dict[str, object]]]:
    profile = _profile(channel_profile_id)
    window = resolve_time_window(prompt, profile.timezone)
    if window is not None:
        return _windowed_performance(channel_profile_id, window)

    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        edit = session.scalar(
            select(EditBlueprintPerformanceSnapshot)
            .where(
                EditBlueprintPerformanceSnapshot.channel_profile_id
                == channel_profile_id
            )
            .order_by(EditBlueprintPerformanceSnapshot.created_at.desc())
            .limit(1)
        )
        packaging = session.scalar(
            select(PackagingIntelligenceSnapshot)
            .where(
                PackagingIntelligenceSnapshot.channel_profile_id
                == channel_profile_id
            )
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


def channel_status(
    channel_profile_id: uuid.UUID,
) -> tuple[str, list[dict[str, object]]]:
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
