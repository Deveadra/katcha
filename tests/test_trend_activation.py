from __future__ import annotations

import inspect
import uuid
from decimal import Decimal
from types import SimpleNamespace

from katcha.api.main import app
from katcha.services.short_episodes import register_short_episode
from katcha.services.trend_activation import (
    _activation_identity,
    _candidate_references,
    derive_episode_signals,
)


def test_activation_routes_are_mounted_without_starting_editorial() -> None:
    paths = app.openapi()["paths"]
    preview_path = (
        "/v1/channels/{channel_profile_id}/trends/opportunities/"
        "{opportunity_id}/activation"
    )
    activate_path = (
        "/v1/channels/{channel_profile_id}/trends/opportunities/"
        "{opportunity_id}/activate"
    )
    assert "get" in paths[preview_path]
    assert "post" in paths[activate_path]


def test_feature_derivation_is_deterministic_and_bounded() -> None:
    clip_id = uuid.uuid4()
    features = SimpleNamespace(
        candidate_score=Decimal("62.5"),
        score_breakdown={
            "ai_hook": 90,
            "ai_surprise": 80,
            "ai_humor": 55,
            "ai_comments": 75,
            "ai_rewatch": 85,
            "visual_change": 70,
            "duration_fit": 95,
            "source_reach": 60,
            "source_engagement": 40,
        },
    )

    first, metadata = derive_episode_signals(clip_id, features)
    second, _ = derive_episode_signals(clip_id, features)

    assert first == second
    assert first.hook_strength == 90
    assert first.visual_clarity == 82.5
    assert first.payoff_strength == 85
    assert first.escalation_value == round((80 + 85 + 70) / 3, 4)
    assert first.commentary_opportunity == 75
    assert first.novelty == 80
    assert first.source_quality == 50
    assert metadata["algorithm"] == "trend-opportunity-activation-v1"
    assert all(
        0 <= value <= 100
        for key, value in first.snapshot().items()
        if key != "clip_id"
    )


def test_feature_derivation_falls_back_to_candidate_score_without_ai() -> None:
    clip_id = uuid.uuid4()
    features = SimpleNamespace(
        candidate_score=Decimal("58"),
        score_breakdown={},
    )

    signals, metadata = derive_episode_signals(clip_id, features)

    assert {
        signals.hook_strength,
        signals.visual_clarity,
        signals.payoff_strength,
        signals.escalation_value,
        signals.commentary_opportunity,
        signals.novelty,
        signals.source_quality,
    } == {58.0}
    assert metadata["fallback"] == "candidate_score"


def test_candidate_reference_extraction_deduplicates_and_reports_invalid_ids() -> None:
    valid = str(uuid.uuid4())
    packet = SimpleNamespace(
        media_refs=[
            {"discovery_candidate_id": valid},
            {"discovery_candidate_id": valid},
            {"discovery_candidate_id": "not-a-uuid"},
            {"kind": "external_url"},
        ]
    )

    references, excluded = _candidate_references(packet)

    assert references == [valid]
    assert len(excluded) == 1
    assert excluded[0].reason == "invalid_discovery_candidate_id"


def test_activation_identity_changes_with_evidence_or_plan_shape() -> None:
    channel = uuid.uuid4()
    opportunity = uuid.uuid4()
    first = _activation_identity(
        channel, opportunity, "a" * 64, "ranksnaxx_countdown", "1.0.0", 5
    )
    same = _activation_identity(
        channel, opportunity, "a" * 64, "ranksnaxx_countdown", "1.0.0", 5
    )
    changed = _activation_identity(
        channel, opportunity, "b" * 64, "ranksnaxx_countdown", "1.0.0", 5
    )

    assert first == same
    assert first != changed


def test_short_episode_registration_accepts_frozen_planning_metadata() -> None:
    parameters = inspect.signature(register_short_episode).parameters
    assert "planning_metadata" in parameters
    assert parameters["planning_metadata"].default is None
