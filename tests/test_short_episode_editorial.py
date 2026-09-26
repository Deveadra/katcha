import uuid
from types import SimpleNamespace

import katcha.api.main
import katcha.api.short_episode_schemas
import katcha.api.short_episodes
import katcha.editorial.episode_generator
import katcha.editorial.episode_schemas
import katcha.services.short_episode_reviews
import katcha.short_episode_models

PLAN = {
    "premise": "Most avoidable public fails",
    "format_key": "ranksnaxx_countdown",
    "format_version": "1.0.0",
    "item_count": 5,
    "ordered_items": [
        {"position": 5, "candidate_id": "clip-5", "role": "opener"},
        {"position": 4, "candidate_id": "clip-4", "role": "build"},
        {"position": 3, "candidate_id": "clip-3", "role": "build"},
        {"position": 2, "candidate_id": "clip-2", "role": "false_peak"},
        {"position": 1, "candidate_id": "clip-1", "role": "payoff"},
    ],
}


def _script_set(*, tamper: bool = False) -> katcha.editorial.episode_schemas.RankedEpisodeScriptSet:
    candidates = []
    for style in ("observational", "sarcastic", "interactive"):
        items = []
        for position in range(5, 0, -1):
            clip_id = f"clip-{position}"
            if tamper and style == "sarcastic" and position == 2:
                clip_id = "invented-clip"
            items.append(
                {
                    "position": position,
                    "clip_id": clip_id,
                    "reveal_line": f"Here is #{position}.",
                    "editorial_purpose": "advance the countdown",
                }
            )
        candidates.append(
            {
                "style": style,
                "title_angle": "A coherent countdown",
                "use_native_cold_open": False,
                "opening_line": "These somehow get worse as we go.",
                "items": items,
                "closing_line": "That last one earned it.",
                "interaction_prompt": (
                    "Which one did you rank differently?" if style == "interactive" else None
                ),
                "rationale": "Keep the source clips central while adding a consistent host layer.",
            }
        )
    return katcha.editorial.episode_schemas.RankedEpisodeScriptSet.model_validate(
        {"candidates": candidates}
    )


def test_editorial_review_and_publication_routes_are_public_api_contracts() -> None:
    paths = katcha.api.main.app.openapi()["paths"]

    assert "/v1/short-episodes/{short_episode_id}/editorial" in paths
    assert "/v1/short-episodes/{short_episode_id}/review" in paths
    assert "/v1/short-episodes/{short_episode_id}/publications" in paths


def test_editorial_workflow_identity_is_stage_scoped() -> None:
    planning_id = "short-episode-abc123"

    script_id = katcha.api.short_episodes._editorial_workflow_id(planning_id, "script")
    voice_id = katcha.api.short_episodes._editorial_workflow_id(planning_id, "voice")
    render_id = katcha.api.short_episodes._editorial_workflow_id(planning_id, "render")

    assert script_id == "short-episode-abc123-editorial-script"
    assert voice_id == "short-episode-abc123-editorial-voice"
    assert render_id == "short-episode-abc123-editorial-render"
    assert script_id != planning_id
    assert voice_id != script_id
    assert render_id not in {planning_id, script_id, voice_id}
    assert (
        katcha.api.short_episodes._editorial_workflow_id(planning_id, "script") == script_id
    )


def test_episode_script_must_exactly_preserve_frozen_plan() -> None:
    scripts = _script_set()

    katcha.editorial.episode_generator.validate_episode_scripts_against_plan(scripts, PLAN)

    try:
        katcha.editorial.episode_generator.validate_episode_scripts_against_plan(
            _script_set(tamper=True), PLAN
        )
    except ValueError as exc:
        assert "frozen countdown plan" in str(exc)
    else:
        raise AssertionError("tampered episode script unexpectedly passed frozen-plan validation")


def test_narration_beats_follow_countdown_and_put_interaction_last() -> None:
    interactive = next(
        candidate for candidate in _script_set().candidates if candidate.style == "interactive"
    )

    beats = interactive.narration_beats()
    ranked_positions = [
        beat["position"] for beat in beats if beat["placement"] == "reveal"
    ]

    assert beats[0]["placement"] == "opening"
    assert ranked_positions == [5, 4, 3, 2, 1]
    assert beats[-1]["placement"] == "interaction"
    assert beats[-1]["position"] is None


def test_native_cold_open_may_omit_opening_line() -> None:
    payload = _script_set().model_dump(mode="json")
    payload["candidates"][0]["use_native_cold_open"] = True
    payload["candidates"][0]["opening_line"] = None

    parsed = katcha.editorial.episode_schemas.RankedEpisodeScriptSet.model_validate(payload)

    assert parsed.candidates[0].use_native_cold_open is True
    assert parsed.candidates[0].narration_beats()[0]["placement"] == "reveal"


def test_regeneration_contract_includes_render_without_repaying_editorial() -> None:
    request = katcha.api.short_episode_schemas.ReviewShortEpisodeRequest(
        decision="regenerate",
        regenerate_from="render",
        actor="operator",
    )

    assert request.regenerate_from == "render"
    assert {"script", "voice", "render"} == katcha.services.short_episode_reviews.REGENERATE_STAGES


def test_regeneration_creates_a_new_episode_execution_identity() -> None:
    channel_profile_id = uuid.uuid4()

    first = katcha.services.short_episode_reviews._regeneration_workflow_id(
        channel_profile_id
    )
    second = katcha.services.short_episode_reviews._regeneration_workflow_id(
        channel_profile_id
    )

    assert first.startswith(f"short-episode-{channel_profile_id.hex[:8]}-")
    assert second.startswith(f"short-episode-{channel_profile_id.hex[:8]}-")
    assert first != second


def test_episode_models_preserve_regeneration_render_and_trend_lineage() -> None:
    episode_columns = set(katcha.short_episode_models.ShortEpisode.__table__.columns.keys())
    review_columns = set(
        katcha.short_episode_models.ShortEpisodeReview.__table__.columns.keys()
    )

    assert {
        "parent_episode_id",
        "generation",
        "regenerate_from",
        "trend_opportunity_id",
        "render_manifest",
    } <= episode_columns
    assert {"short_episode_id", "decision", "actor", "review_metadata"} <= review_columns


def test_fixture_editorial_does_not_require_provider_keys(monkeypatch) -> None:
    settings = SimpleNamespace(
        ai_enabled=True,
        openai_api_key=None,
        gemini_api_key=None,
        resolved_ai_execution_mode=lambda: "fixture",
    )
    monkeypatch.setattr(
        katcha.api.short_episodes,
        "get_settings",
        lambda: settings,
    )

    katcha.api.short_episodes._require_ai_execution()
