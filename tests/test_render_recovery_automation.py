from __future__ import annotations

import inspect
from datetime import UTC, datetime

import katcha.render_models
from katcha.orchestration import production_workflows, short_episode_workflows
from katcha.services import productions
from katcha.services.render_automation import (
    _green_snapshot,
    _next_slot_candidates,
)


def test_render_attempt_model_has_durable_source_and_dead_letter_fields() -> None:
    columns = set(katcha.render_models.RenderAttempt.__table__.columns.keys())
    assert {
        "attempt_key",
        "production_id",
        "short_episode_id",
        "parent_attempt_id",
        "source_generation",
        "attempt_number",
        "status",
        "stage",
        "output_key",
        "manifest_version",
        "verification",
        "failure_count",
        "last_failure_class",
        "error",
        "started_at",
        "completed_at",
    } <= columns

    names = {
        constraint.name
        for constraint in katcha.render_models.RenderAttempt.__table__.constraints
        if constraint.name
    }
    assert "ck_render_attempts_exactly_one_source" in names
    assert "uq_render_attempts_production_attempt" in names
    assert "uq_render_attempts_short_episode_attempt" in names


def test_low_risk_requires_both_green_lane_and_current_eligibility() -> None:
    assert _green_snapshot({"eligible": True, "rights_lane": "green"})
    assert not _green_snapshot({"eligible": False, "rights_lane": "green"})
    assert not _green_snapshot({"eligible": True, "rights_lane": "yellow"})
    assert not _green_snapshot({"rights_lane": "green"})


def test_schedule_candidates_are_future_and_respect_blackout_slots() -> None:
    now = datetime(2026, 9, 24, 8, 30, tzinfo=UTC)
    candidates = _next_slot_candidates(
        now=now,
        timezone="UTC",
        slots=[(3, 10), (4, 9)],
        blackouts={(3, 10)},
        weeks=2,
    )

    assert candidates
    assert all(candidate > now for candidate in candidates)
    assert all(
        not (candidate.weekday() == 3 and candidate.hour == 10)
        for candidate in candidates
    )
    assert candidates[0].weekday() == 4
    assert candidates[0].hour == 9


def test_text_only_render_recovery_does_not_require_fake_voice_assets() -> None:
    source = inspect.getsource(productions._clone_voice_assets)
    assert '"text_only", "source_only"' in source
    assert "parent production has no narration assets to reuse" in source


def test_production_workflow_hands_verified_render_to_automation_layer() -> None:
    source = inspect.getsource(production_workflows.ShortProductionWorkflow.run)
    assert "advance_production_render_automation_activity" in source
    assert "start_registered_publication_activity" in source
    assert "publication_handoff_started" in source


def test_ranked_episode_workflow_can_advance_both_review_gates() -> None:
    source = inspect.getsource(
        short_episode_workflows.RankedShortEpisodeEditorialWorkflow.run
    )
    assert "advance_short_episode_editorial_automation_activity" in source
    assert "advance_short_episode_render_automation_activity" in source
    assert "start_registered_publication_activity" in source
