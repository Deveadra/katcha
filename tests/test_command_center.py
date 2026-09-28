import uuid
from datetime import UTC, datetime

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


def test_command_center_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/ai/command" in paths
    assert "/v1/ai/actions/{proposal_id}" in paths
    assert "/v1/ai/actions/{proposal_id}/execute" in paths
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
    assert "never confirms" in str(resolution.resolution).casefold()


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
