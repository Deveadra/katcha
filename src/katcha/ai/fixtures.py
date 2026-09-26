from __future__ import annotations

from katcha.ai.schemas import (
    ClipVisionResult,
    DeepVideoResult,
    PackagingCandidateSet,
)
from katcha.editorial.episode_schemas import RankedEpisodeScriptSet
from katcha.editorial.schemas import ShortScriptSet
from katcha.longform.schemas import (
    CandidateEvidence,
    FinalLongformPlan,
    LongformCritique,
    LongformEditorPlan,
)

def fixture_clip_vision(reference_id: str) -> ClipVisionResult:
    seed = sum(reference_id.encode("utf-8"))
    hook = 58 + (seed % 19)
    return ClipVisionResult(
        event_summary=(
            "Deterministic development fixture analysis. "
            "No external AI provider was called."
        ),
        categories=["development_fixture"],
        tone=["neutral", "synthetic"],
        setup="Synthetic fixture source prepared for end-to-end pipeline testing.",
        payoff="Fixture reaches its deterministic test payoff.",
        hook_score=float(hook),
        surprise_score=float(55 + seed % 17),
        humor_score=float(50 + seed % 21),
        comment_potential=float(57 + seed % 18),
        rewatch_potential=float(54 + seed % 20),
        context_required=False,
        requires_deep_video=False,
        deep_video_reason=None,
        confidence=1.0,
    )


def fixture_deep_video(reference_id: str) -> DeepVideoResult:
    base = fixture_clip_vision(reference_id).model_dump()
    return DeepVideoResult(
        **base,
        timeline=[
            "Synthetic fixture begins.",
            "Fixture test pattern progresses.",
            "Synthetic fixture reaches its planned endpoint.",
        ],
        audio_relevance="Synthetic development audio only.",
        best_commentary_moments=["After the fixture opener", "Before the fixture payoff"],
    )


def fixture_short_scripts() -> ShortScriptSet:
    return ShortScriptSet.model_validate(
        {
            "candidates": [
                {
                    "style": "observational",
                    "segments": [
                        {
                            "placement": "pre",
                            "text": "Development fixture voiceover starts here.",
                            "purpose": "exercise pre-clip narration",
                        }
                    ],
                    "title_angle": "Fixture observational treatment",
                    "rationale": "Deterministic zero-cost development script.",
                },
                {
                    "style": "sarcastic",
                    "segments": [
                        {
                            "placement": "post",
                            "text": "Yep. The test clip survived the pipeline.",
                            "purpose": "exercise post-clip narration",
                        }
                    ],
                    "title_angle": "Fixture sarcastic treatment",
                    "rationale": "Deterministic zero-cost development script.",
                },
                {
                    "style": "interactive",
                    "segments": [
                        {
                            "placement": "post",
                            "text": "Fixture complete. Your turn to judge the pipeline.",
                            "purpose": "exercise interaction narration",
                        }
                    ],
                    "interaction_prompt": "Which fixture stage would you inspect next?",
                    "title_angle": "Fixture interactive treatment",
                    "rationale": "Deterministic zero-cost development script.",
                },
            ]
        }
    )


def fixture_ranked_episode_scripts(
    plan_snapshot: dict[str, object],
) -> RankedEpisodeScriptSet:
    planned_items = sorted(
        list(plan_snapshot.get("ordered_items") or []),
        key=lambda item: int(item["position"]),
        reverse=True,
    )
    if not planned_items:
        raise ValueError("fixture ranked episode requires planned items")

    treatments = []
    for style in ("observational", "sarcastic", "interactive"):
        items = []
        for raw in planned_items:
            position = int(raw["position"])
            clip_id = str(raw["candidate_id"])
            role = str(raw.get("role") or "ranked")
            if style == "observational":
                line = f"Fixture number {position}: checking the {role} slot."
            elif style == "sarcastic":
                line = f"Number {position}. Very dramatic for a development fixture."
            else:
                line = f"Number {position}. Keep it here, or move it?"
            items.append(
                {
                    "position": position,
                    "clip_id": clip_id,
                    "reveal_line": line,
                    "editorial_purpose": f"exercise {role} countdown placement",
                }
            )

        treatments.append(
            {
                "style": style,
                "title_angle": f"Zero-cost {style} fixture countdown",
                "use_native_cold_open": False,
                "opening_line": "RankSnaxx development fixture: countdown test starts now.",
                "items": items,
                "closing_line": "Fixture countdown complete. The real render path is next.",
                "interaction_prompt": (
                    "Which fixture position would you change?"
                    if style == "interactive"
                    else None
                ),
                "rationale": (
                    "Deterministic fixture script used to validate orchestration, "
                    "voice, editing, rendering, and review without paid inference."
                ),
            }
        )

    return RankedEpisodeScriptSet.model_validate({"candidates": treatments})


def fixture_packaging_candidates(
    context: dict[str, object],
    candidate_count: int,
) -> PackagingCandidateSet:
    facts = [
        str(value).strip()
        for value in context.get("grounding_facts") or []
        if str(value).strip()
    ]
    if not facts:
        raise ValueError("fixture packaging requires at least one grounding fact")
    current_title = str(context.get("current_title") or "Fixture video").strip()
    families = ("curiosity", "payoff", "ranking", "contrast", "reaction")
    candidates = []
    for index in range(candidate_count):
        family = families[index]
        title = f"[DEV {index + 1}] {current_title}"[:100]
        candidates.append(
            {
                "variation_family": family,
                "angle": f"Deterministic {family} packaging fixture",
                "title": title,
                "description": (
                    "Development fixture packaging generated without an external AI provider."
                ),
                "supporting_facts": [facts[index % len(facts)]],
                "thumbnail": {
                    "concept": f"Development fixture thumbnail concept {index + 1}",
                    "focal_subject": "synthetic acceptance media",
                    "composition": "single centered fixture subject with clear ranking emphasis",
                    "on_image_text": f"DEV {index + 1}",
                    "emotion": "clear test signal",
                    "avoid": ["production publishing"],
                },
            }
        )
    return PackagingCandidateSet.model_validate({"candidates": candidates})


def fixture_longform_editor_plan(
    candidates: list[CandidateEvidence],
    *,
    min_segments: int,
) -> LongformEditorPlan:
    required = max(3, min_segments)
    if len(candidates) < required:
        raise ValueError(
            f"fixture long-form plan requires at least {required} candidates"
        )
    selected = candidates[:required]
    return LongformEditorPlan.model_validate(
        {
            "title_angle": "Development fixture compilation",
            "opening_hook": "Development fixture: validating the long-form edit path.",
            "intro": None,
            "segments": [
                {
                    "clip_id": str(item.clip_id),
                    "host_before": f"Fixture segment {index + 1}.",
                    "host_after": None,
                    "transition_before": None,
                    "source_start_seconds": 0,
                    "source_end_seconds": float(item.duration_seconds),
                    "reason": "Deterministic zero-cost development segment.",
                }
                for index, item in enumerate(selected)
            ],
            "outro": "Development fixture compilation complete.",
            "pacing_notes": ["Fixture mode preserves deterministic segment order."],
        }
    )


def fixture_longform_critique() -> LongformCritique:
    return LongformCritique(
        verdict="pass",
        strengths=["Deterministic fixture plan is structurally valid."],
        issues=[],
        pacing_summary="Fixture critique skips external inference.",
        audience_fit_summary="Development-only acceptance artifact.",
    )


def fixture_final_longform_plan(plan: LongformEditorPlan) -> FinalLongformPlan:
    return FinalLongformPlan(
        **plan.model_dump(),
        revision_summary="Deterministic fixture finalization; no external AI provider called.",
    )
