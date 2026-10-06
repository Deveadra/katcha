import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from katcha.ai.command_planner import CommandPlan
from katcha.api import command_center as command_api
from katcha.api.command_center import CommandRequest, _action_specs
from katcha.api.main import app
from katcha.command_center_models import (
    CommandActionProposal,
    CommandThread,
    CommandTurn,
)
from katcha.editorial.rankings import (
    RankingCandidateSignals,
    build_locked_ranking_episode_plan,
    rank_snaxx_countdown_v1,
)
from katcha.services.command_center import (
    _search_terms,
    classify_intent,
    infer_edit_blueprint_key,
    resolve_command_follow_up,
    resolve_time_window,
)
from katcha.services.ingestion_sources import upsert_ingestion_source


def test_command_center_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/ai/command" in paths
    assert "/v1/ai/actions/{proposal_id}" in paths
    assert "/v1/ai/actions/{proposal_id}/activity" in paths
    assert "/v1/ai/actions/{proposal_id}/execute" in paths
    assert "/v1/ai/observability" in paths
    assert "/v1/ai/threads" in paths
    assert "/v1/ai/threads/{thread_id}" in paths
    assert "/v1/ai/threads/{thread_id}/archive" in paths
    assert "/v1/ai/actions/execute" not in paths


def test_command_center_classifies_operator_examples() -> None:
    assert classify_intent("What's currently failing?", []) == "failures"
    assert (
        classify_intent(
            "Show me the best Xbox clips found today and explain why they scored highly.",
            [],
        )
        == "best_clips"
    )
    assert (
        classify_intent(
            "Look at yesterday's performance and tell me what editing behavior should change.",
            [],
        )
        == "performance_advice"
    )
    assert (
        classify_intent(
            "Make a RankSnaxx episode from these five clips using the commentary recipe.",
            [uuid.uuid4() for _ in range(5)],
        )
        == "create_content"
    )
    assert classify_intent("Why did Katcha reject this clip?", [uuid.uuid4()]) == "clip_rejection"
    assert (
        classify_intent("Add TikTok sources to pull content from.", [])
        == "source_discovery"
    )
    assert (
        classify_intent(
            "Find new sources across Instagram, X, and Bluesky.",
            [],
        )
        == "source_discovery"
    )


    assert (
        classify_intent(
            "Look for funny fails that can be used in a rank video.",
            [],
        )
        == "source_discovery"
    )
    assert (
        classify_intent(
            "Find funny fails that would work in a ranked video.",
            [],
        )
        == "source_discovery"
    )
    assert (
        classify_intent("Find the best clips found today.", [])
        == "best_clips"
    )
    assert (
        classify_intent("Look for why this render failed.", [])
        == "failures"
    )
    assert (
        classify_intent(
            "Get the official trailers for VisionQuest from Marvel's YouTube "
            "channel and prepare it for production.",
            [],
        )
        == "source_discovery"
    )


def test_source_scout_proposal_freezes_discovery_scope() -> None:
    request = CommandRequest(
        channel_profile_id=uuid.uuid4(),
        prompt="Find new gaming sources across TikTok and Bluesky.",
    )

    specs = _action_specs(
        request,
        "source_discovery",
        [
            {
                "kind": "source_discovery",
                "id": "current",
                "web_scout_ready": True,
                "requested_platforms": ["tiktok", "bluesky"],
                "suggested_terms": ["gaming"],
            }
        ],
    )

    assert len(specs) == 1
    assert specs[0].action_type == "start_source_scout"
    assert specs[0].payload["platforms"] == ["tiktok", "bluesky"]
    assert specs[0].payload["terms"] == ["gaming"]
    assert specs[0].payload["interval_minutes"] == 60


def test_named_official_source_becomes_a_bounded_executable_search() -> None:
    source = upsert_ingestion_source(
        source_key=f"marvel-fixture-{uuid.uuid4()}",
        name="Marvel Entertainment",
        adapter_key="youtube",
        adapter_version="v1",
        platform="youtube",
        query_template={"channel_reference": "@marvel"},
        enabled=True,
    )
    request = CommandRequest(
        channel_profile_id=uuid.uuid4(),
        prompt=(
            "Get the official trailers for VisionQuest from Marvel's YouTube "
            "channel and prepare it for production."
        ),
    )
    plan = CommandPlan(
        intent="source_discovery",
        confidence=0.99,
        reason="Search a configured official source and prepare the matches.",
        source_hint="Marvel Entertainment",
        search_query="VisionQuest official trailer",
        prepare_for_production=True,
    )

    specs = _action_specs(
        request,
        "source_discovery",
        [
            {
                "kind": "source_discovery",
                "id": "current",
                "web_scout_ready": True,
                "requested_platforms": ["youtube"],
                "suggested_terms": ["visionquest", "marvel"],
            }
        ],
        plan=plan,
    )

    assert len(specs) == 1
    assert specs[0].action_type == "start_source_scout"
    assert specs[0].payload["source_id"] == str(source.id)
    assert specs[0].payload["source_name"] == "Marvel Entertainment"
    assert specs[0].payload["query_overrides"]["q"] == "VisionQuest official trailer"
    assert specs[0].payload["prepare_for_production"] is True
    assert "review pipeline" in specs[0].description


def test_operational_follow_up_confirms_the_prior_frozen_action() -> None:
    turns, assistant_id = _conversation_turns_with_clips([uuid.uuid4()])
    turns[-1].intent = "source_discovery"

    resolution = resolve_command_follow_up(
        "Proceed with the operational next step you defined.",
        [],
        turns,
        has_pending_proposal=True,
    )

    assert resolution.intent_hint == "confirm_action"
    assert resolution.action_source_turn_id == assistant_id
    assert resolution.inherited_from_thread is True
    assert "frozen action" in str(resolution.resolution).casefold()


@pytest.mark.asyncio
async def test_named_source_action_starts_search_and_prepare_workflow(
    monkeypatch,
) -> None:
    source = upsert_ingestion_source(
        source_key=f"marvel-execute-{uuid.uuid4()}",
        name="Marvel Entertainment",
        adapter_key="youtube",
        adapter_version="v1",
        platform="youtube",
        query_template={"channel_reference": "@marvel"},
        enabled=True,
    )
    run_id = uuid.uuid4()
    captured = {}

    def create_run(source_id, **kwargs):
        captured["source_id"] = source_id
        captured.update(kwargs)
        return SimpleNamespace(id=run_id)

    async def start_prepare(run_id_value, workflow_id, *, ingest_task_queue):
        captured["run_id"] = run_id_value
        captured["workflow_id"] = workflow_id
        captured["ingest_task_queue"] = ingest_task_queue
        return workflow_id

    monkeypatch.setattr(
        command_api,
        "create_discovery_run_from_source",
        create_run,
    )
    monkeypatch.setattr(
        command_api,
        "start_command_source_prepare_workflow",
        start_prepare,
    )
    monkeypatch.setattr(
        command_api,
        "get_settings",
        lambda: SimpleNamespace(temporal_task_queue="katcha-media"),
    )

    proposal = SimpleNamespace(
        id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        channel_profile_id=uuid.uuid4(),
        action_type="start_source_scout",
        payload={
            "source_id": str(source.id),
            "source_name": source.name,
            "query_overrides": {
                "q": "VisionQuest official trailer",
                "order": "relevance",
                "freshness_horizon_hours": 0,
                "limit": 25,
            },
            "prepare_for_production": True,
            "operator_request": (
                "Get the official trailers for VisionQuest from Marvel's "
                "YouTube channel and prepare it for production."
            ),
            "search_query": "VisionQuest official trailer",
        },
    )

    result = await command_api._execute_proposal(
        proposal,  # type: ignore[arg-type]
        actor="control-principal:operator",
    )

    assert captured["source_id"] == source.id
    assert captured["query_overrides"]["q"] == "VisionQuest official trailer"
    assert captured["metadata"]["command_prepare_for_production"] is True
    assert captured["metadata"]["command_match_terms"] == ["visionquest"]
    assert captured["run_id"] == str(run_id)
    assert captured["ingest_task_queue"] == "katcha-media"
    assert result["prepare_for_production"] is True
    assert result["workflow_id"] == f"command-source-prepare-{run_id}"


def test_selected_clip_explanation_is_read_only_intent() -> None:
    selected = [uuid.uuid4()]

    assert (
        classify_intent("Explain why this scored highly.", selected)
        == "clip_explanation"
    )


def test_operator_explanation_words_do_not_become_clip_filters() -> None:
    terms = _search_terms(
        "Show me the best Xbox clips found today and explain why they scored highly."
    )

    assert terms == ["xbox"]


def test_natural_discovery_prompt_keeps_only_topic_terms() -> None:
    terms = _search_terms(
        "Look for funny fails that can be used in a rank video."
    )

    assert terms == ["funny", "fails"]


def test_command_center_maps_plain_language_recipe_names() -> None:
    assert infer_edit_blueprint_key("use the commentary recipe") == "persona_commentary"
    assert infer_edit_blueprint_key("use the header explainer") == "header_explainer"
    assert infer_edit_blueprint_key("use the channel default") is None


def test_yesterday_resolves_to_a_closed_channel_local_day(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 9, 28, 13, 0, tzinfo=UTC)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr("katcha.services.command_center.datetime", FixedDateTime)
    window = resolve_time_window(
        "look at yesterday's performance",
        "America/Chicago",
    )

    assert window is not None
    assert window.start == datetime(2026, 9, 27, 5, 0, tzinfo=UTC)
    assert window.end == datetime(2026, 9, 28, 5, 0, tzinfo=UTC)


def test_locked_ranked_episode_preserves_operator_clip_order() -> None:
    ids = [uuid.uuid4() for _ in range(5)]
    candidates = [
        RankingCandidateSignals(
            candidate_id=str(clip_id),
            hook_strength=50 + index,
            visual_clarity=60,
            payoff_strength=70,
            escalation_value=65,
            commentary_opportunity=80,
            novelty=55,
            source_quality=75,
            rights_ready=True,
        )
        for index, clip_id in enumerate(ids)
    ]

    plan = build_locked_ranking_episode_plan(
        candidates,
        premise="Five fixture clips",
        item_count=5,
        contract=rank_snaxx_countdown_v1(),
    )

    assert [item.candidate_id for item in plan.ordered_items] == [
        str(value) for value in ids
    ]
    assert [item.position for item in plan.ordered_items] == [5, 4, 3, 2, 1]
    assert plan.ordered_items[0].role == "opener"
    assert plan.ordered_items[-1].role == "payoff"


def test_action_proposal_model_freezes_server_authority() -> None:
    columns = CommandActionProposal.__table__.c

    assert columns.idempotency_key.unique is True
    assert columns.channel_profile_id.nullable is False
    assert columns.payload.nullable is False
    assert columns.status.nullable is False
    assert columns.confirmed_by.nullable is True
    assert columns.expires_at.nullable is False
    assert columns.thread_id.nullable is True
    assert columns.source_turn_id.nullable is True


def test_command_history_models_preserve_order_and_channel_scope() -> None:
    thread_columns = CommandThread.__table__.c
    turn_columns = CommandTurn.__table__.c

    assert thread_columns.channel_profile_id.nullable is False
    assert thread_columns.created_by.nullable is False
    assert thread_columns.last_activity_at.nullable is False
    assert turn_columns.thread_id.nullable is False
    assert turn_columns.channel_profile_id.nullable is False
    assert turn_columns.sequence_number.nullable is False
    assert turn_columns.content.nullable is False



def _conversation_turns_with_clips(
    clip_ids: list[uuid.UUID],
) -> tuple[list[CommandTurn], uuid.UUID]:
    request_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    user = CommandTurn(
        id=uuid.uuid4(),
        thread_id=thread_id,
        channel_profile_id=uuid.uuid4(),
        sequence_number=1,
        role="user",
        request_id=request_id,
        content="Show me the best Xbox clips found today.",
        evidence=[],
        turn_context={"selected_clip_ids": []},
    )
    assistant_id = uuid.uuid4()
    assistant = CommandTurn(
        id=assistant_id,
        thread_id=thread_id,
        channel_profile_id=user.channel_profile_id,
        sequence_number=2,
        role="assistant",
        request_id=request_id,
        intent="best_clips",
        narrator="fixture/grounded-command-v1",
        content="Here are the best Xbox clips.",
        evidence=[
            {
                "kind": "clip",
                "id": str(clip_id),
                "rank": index + 1,
                "title": f"Clip {index + 1}",
            }
            for index, clip_id in enumerate(clip_ids)
        ],
        turn_context={},
    )
    return [user, assistant], assistant_id


def test_conversation_follow_up_resolves_clip_references_deterministically() -> None:
    clips = [uuid.uuid4() for _ in range(5)]
    turns, assistant_id = _conversation_turns_with_clips(clips)

    why = resolve_command_follow_up("Why?", [], turns)
    assert why.selected_clip_ids == (clips[0],)
    assert why.inherited_from_thread is True
    assert why.source_turn_id == assistant_id

    second = resolve_command_follow_up(
        "Turn the second one into a short.",
        [],
        turns,
    )
    assert second.selected_clip_ids == (clips[1],)

    plural = resolve_command_follow_up(
        "Make those into an episode.",
        [],
        turns,
    )
    assert plural.selected_clip_ids == tuple(clips)


def test_conversation_follow_up_inherits_topic_for_time_shift() -> None:
    clips = [uuid.uuid4()]
    turns, assistant_id = _conversation_turns_with_clips(clips)

    resolution = resolve_command_follow_up(
        "What about yesterday?",
        [],
        turns,
    )

    assert resolution.intent_hint == "best_clips"
    assert resolution.selected_clip_ids == ()
    assert resolution.inherited_from_thread is True
    assert resolution.source_turn_id == assistant_id
    assert "xbox" in resolution.effective_prompt.casefold()
    assert "yesterday" in resolution.effective_prompt.casefold()


def test_conversation_follow_up_never_treats_chat_confirmation_as_execution() -> None:
    turns, assistant_id = _conversation_turns_with_clips([uuid.uuid4()])

    resolution = resolve_command_follow_up(
        "Yes, do it.",
        [],
        turns,
        has_pending_proposal=True,
    )

    assert resolution.intent_hint == "confirm_action"
    assert resolution.action_source_turn_id == assistant_id
    assert "frozen action" in str(resolution.resolution).casefold()


def test_explicit_clip_selection_wins_over_conversation_reference() -> None:
    prior = [uuid.uuid4(), uuid.uuid4()]
    turns, _ = _conversation_turns_with_clips(prior)
    explicit = uuid.uuid4()

    resolution = resolve_command_follow_up(
        "Why?",
        [explicit],
        turns,
        has_pending_proposal=True,
    )

    assert resolution.selected_clip_ids == (explicit,)
    assert resolution.inherited_from_thread is False
    assert resolution.intent_hint is None


def test_typed_confirmation_stays_gated_with_explicit_clip_context() -> None:
    prior = [uuid.uuid4()]
    turns, assistant_id = _conversation_turns_with_clips(prior)
    explicit = uuid.uuid4()

    resolution = resolve_command_follow_up(
        "Yes, do it.",
        [explicit],
        turns,
        has_pending_proposal=True,
    )

    assert resolution.intent_hint == "confirm_action"
    assert resolution.action_source_turn_id == assistant_id
    assert resolution.selected_clip_ids == (explicit,)


def test_turn_into_short_is_create_content_intent() -> None:
    selected = [uuid.uuid4()]

    assert classify_intent("Turn that into a short.", selected) == "create_content"



def test_command_center_accepts_typed_resource_context_schema() -> None:
    from katcha.api.command_center import CommandRequest

    clip_id = uuid.uuid4()
    request = CommandRequest.model_validate(
        {
            "channel_profile_id": str(uuid.uuid4()),
            "prompt": "Explain this.",
            "resource_refs": [
                {"kind": "clip", "id": str(clip_id)},
            ],
        }
    )

    assert request.resource_refs[0].kind == "clip"
    assert request.resource_refs[0].id == clip_id

    project_id = uuid.uuid4()
    editorial = CommandRequest.model_validate(
        {
            "channel_profile_id": str(uuid.uuid4()),
            "prompt": "Inspect this Editorial project.",
            "resource_refs": [
                {"kind": "editorial_project", "id": str(project_id)},
            ],
        }
    )
    assert editorial.resource_refs[0].kind == "editorial_project"
    assert editorial.resource_refs[0].id == project_id


@pytest.mark.parametrize("prompt", ["Hi!", "Hello", "What can you do?", "How can you help?"])
def test_basic_conversation_has_a_read_only_route(prompt):
    assert classify_intent(prompt, []) == "conversation"
