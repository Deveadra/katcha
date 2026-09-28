import uuid

from katcha.api.main import app
from katcha.services.command_center import classify_intent


def test_command_center_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/ai/command" in paths
    assert "/v1/ai/actions/execute" in paths


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
