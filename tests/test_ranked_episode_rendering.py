import pytest

import katcha.api.schemas
import katcha.publishing_models
from katcha.rendering.manifest import channel_01_brand_v1
from katcha.rendering.ranked_episode_manifest import build_ranked_episode_manifest


def _items(count: int = 5) -> list[dict[str, object]]:
    roles = {
        5: "opener",
        4: "build",
        3: "build",
        2: "false_peak",
        1: "payoff",
    }
    return [
        {
            "position": position,
            "role": roles[position],
            "clip_id": f"clip-{position}",
            "storage_key": f"clips/{position}.mp4",
            "source_duration_seconds": 9.0,
            "width": 1080,
            "height": 1920,
            "native_audio_policy": "duck",
        }
        for position in range(count, 0, -1)
    ]


def _narration() -> list[dict[str, object]]:
    return [
        {
            "sequence": 0,
            "storage_key": "audio/open.wav",
            "placement": "opening",
            "position": None,
            "clip_id": None,
            "text": "These get worse as we go.",
            "duration_seconds": 0.8,
        },
        {
            "sequence": 1,
            "storage_key": "audio/five.wav",
            "placement": "reveal",
            "position": 5,
            "clip_id": "clip-5",
            "text": "Number five.",
            "duration_seconds": 0.45,
        },
        {
            "sequence": 2,
            "storage_key": "audio/one.wav",
            "placement": "post_clip",
            "position": 1,
            "clip_id": "clip-1",
            "text": "That absolutely earned number one.",
            "duration_seconds": 0.7,
        },
        {
            "sequence": 3,
            "storage_key": "audio/interaction.wav",
            "placement": "interaction",
            "position": None,
            "clip_id": None,
            "text": "Which one did you rank differently?",
            "duration_seconds": 0.8,
        },
    ]


def _manifest():
    return build_ranked_episode_manifest(
        short_episode_id="episode-1",
        premise="Five public fails that somehow keep escalating",
        format_key="ranksnaxx_countdown",
        format_version="1.0.0",
        ordered_items=_items(),
        narration_assets=_narration(),
        selected_style="interactive",
        interaction_prompt="Which one did you rank differently?",
        output_key="short-episodes/episode-1/renders/g1.mp4",
        brand=channel_01_brand_v1(),
        trend_opportunity_id="trend-1",
    )


def test_ranked_episode_manifest_is_deterministic_and_preserves_countdown() -> None:
    first = _manifest()
    second = _manifest()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [item.position for item in first.items] == [5, 4, 3, 2, 1]
    assert [item.role for item in first.items] == [
        "opener",
        "build",
        "build",
        "false_peak",
        "payoff",
    ]
    assert first.items[-1].countdown_label == "#1"
    assert first.items[-1].transition_before == "flash"
    assert first.output_duration_seconds <= 60
    assert first.treatment.trend_opportunity_id == "trend-1"
    assert first.treatment.narration_density > 0


def test_ranked_episode_manifest_fails_closed_over_duration_target() -> None:
    narration = []
    for sequence in range(7):
        narration.append(
            {
                "sequence": sequence,
                "storage_key": f"audio/{sequence}.wav",
                "placement": "opening" if sequence == 0 else "transition",
                "position": None if sequence == 0 else 5,
                "clip_id": None if sequence == 0 else "clip-5",
                "text": "A deliberately long narration beat.",
                "duration_seconds": 9.0,
            }
        )

    with pytest.raises(ValueError, match="60-second"):
        build_ranked_episode_manifest(
            short_episode_id="episode-long",
            premise="Too much narration",
            format_key="ranksnaxx_countdown",
            format_version="1.0.0",
            ordered_items=_items(),
            narration_assets=narration,
            selected_style="observational",
            interaction_prompt=None,
            output_key="short-episodes/episode-long/renders/g1.mp4",
            brand=channel_01_brand_v1(),
        )


def test_publication_contract_has_first_class_short_episode_lineage() -> None:
    columns = set(katcha.publishing_models.Publication.__table__.columns.keys())
    response_fields = set(katcha.api.schemas.PublicationResponse.model_fields)

    assert {"production_id", "compilation_id", "short_episode_id"} <= columns
    assert "treatment_metadata" in columns
    assert {"short_episode_id", "treatment_metadata"} <= response_fields

    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in katcha.publishing_models.Publication.__table__.constraints
        if getattr(constraint, "name", None) == "ck_publications_exactly_one_source"
    )
    assert "short_episode_id" in check_sql
