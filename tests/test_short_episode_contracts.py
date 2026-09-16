import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import UniqueConstraint

from katcha.api.main import app
from katcha.api.short_episode_schemas import ShortEpisodeCandidateRequest
from katcha.services.short_episodes import (
    ShortEpisodeCandidateInput,
    _resolve_format,
    _workflow_id,
)
from katcha.short_episode_models import ShortEpisodeItem


def test_short_episode_api_is_wired_into_control_plane() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/short-episodes" in paths
    assert "/v1/short-episodes/{short_episode_id}" in paths


def test_candidate_request_cannot_claim_rights_readiness() -> None:
    with pytest.raises(ValidationError, match="rights_ready"):
        ShortEpisodeCandidateRequest.model_validate(
            {
                "clip_id": str(uuid.uuid4()),
                "hook_strength": 80,
                "visual_clarity": 80,
                "payoff_strength": 80,
                "escalation_value": 80,
                "commentary_opportunity": 80,
                "novelty": 80,
                "source_quality": 80,
                "rights_ready": True,
            }
        )


def test_internal_candidate_snapshot_keeps_rights_out_of_editorial_signals() -> None:
    candidate = ShortEpisodeCandidateInput(
        clip_id=uuid.uuid4(),
        hook_strength=80,
        visual_clarity=81,
        payoff_strength=82,
        escalation_value=83,
        commentary_opportunity=84,
        novelty=85,
        source_quality=86,
    )

    assert "rights_ready" not in candidate.snapshot()
    assert candidate.ranking_signals().rights_ready is True


def test_format_must_match_active_channel_brand() -> None:
    contract = _resolve_format(
        "ranksnaxx_countdown",
        "1.0.0",
        None,
        None,
    )
    assert contract.key == "ranksnaxx_countdown"

    with pytest.raises(ValueError, match="not enabled"):
        _resolve_format(
            "ranksnaxx_countdown",
            "1.0.0",
            "other_format",
            "1.0.0",
        )


def test_idempotency_key_is_scoped_to_channel() -> None:
    first_channel = uuid.uuid4()
    second_channel = uuid.uuid4()

    assert _workflow_id(first_channel, "episode-42") == _workflow_id(
        first_channel, "episode-42"
    )
    assert _workflow_id(first_channel, "episode-42") != _workflow_id(
        second_channel, "episode-42"
    )


def test_episode_items_enforce_unique_position_and_clip() -> None:
    unique_sets = {
        tuple(constraint.columns.keys())
        for constraint in ShortEpisodeItem.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("short_episode_id", "position") in unique_sets
    assert ("short_episode_id", "clip_id") in unique_sets
