from uuid import uuid4

import pytest
from pydantic import ValidationError

from katcha.longform.schemas import EditorSegmentPlan, LongformEditorPlan


def test_editor_plan_rejects_duplicate_clip_ids() -> None:
    clip_id = uuid4()
    with pytest.raises(ValidationError):
        LongformEditorPlan(
            title_angle="The funniest moments",
            opening_hook="No warm-up. Start with the strongest clip.",
            segments=[
                EditorSegmentPlan(clip_id=clip_id),
                EditorSegmentPlan(clip_id=clip_id),
                EditorSegmentPlan(clip_id=uuid4()),
            ],
        )


def test_editor_plan_accepts_unique_segments() -> None:
    plan = LongformEditorPlan(
        title_angle="Unexpected wins",
        opening_hook="Start immediately.",
        segments=[
            EditorSegmentPlan(clip_id=uuid4(), host_after="That somehow worked."),
            EditorSegmentPlan(clip_id=uuid4()),
            EditorSegmentPlan(clip_id=uuid4(), transition_before="And then it got stranger."),
        ],
    )

    assert len(plan.segments) == 3
