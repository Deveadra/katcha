from sqlalchemy import UniqueConstraint

from katcha.brand_models import ChannelBrandVersion
from katcha.branding import channel_01_brand_v1, validate_brand_contract
from katcha.orchestration.production_activities import (
    _brand_render_spec,
    _brand_voice_profile,
)
from katcha.production_models import Production
from katcha.config import Settings


def test_channel_01_brand_contract_is_complete_and_round_trips() -> None:
    contract = channel_01_brand_v1()
    restored = validate_brand_contract(contract.model_dump(mode="json"))

    assert restored.brand_key == "channel_01"
    assert restored.version == 1
    assert restored.persona.key == "youth_host"
    assert restored.persona.version == "2.0.0"
    assert restored.voice_policy.preferred_profiles == [
        "openai_youth_v2",
        "gemini_youth_v2",
    ]
    assert restored.visual["theme_key"] == "signal_v1"
    assert "official_ruling" in restored.interaction["allowed_rituals"]


def test_brand_versions_are_unique_per_channel_and_version() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in ChannelBrandVersion.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("channel_profile_id", "version") in unique_columns


def test_production_exposes_frozen_brand_lineage() -> None:
    columns = Production.__table__.c

    assert columns.brand_key.nullable is True
    assert columns.brand_version.nullable is True
    assert columns.brand_snapshot.nullable is True


def test_frozen_brand_snapshot_drives_render_tokens() -> None:
    contract = channel_01_brand_v1()
    render_spec = _brand_render_spec(contract.model_dump(mode="json"))

    assert render_spec is not None
    assert render_spec.brand_key == "channel_01"
    assert render_spec.palette.signal_blue == "#5B6CFF"
    assert render_spec.captions.treatment_key == "impact_clean_v1"


def test_frozen_brand_snapshot_prefers_available_branded_voice() -> None:
    contract = channel_01_brand_v1()
    settings = Settings(
        openai_api_key=None,
        gemini_api_key="test-gemini",
    )

    profile = _brand_voice_profile(contract.model_dump(mode="json"), settings)

    assert profile is not None
    assert profile.key == "gemini_youth_v2"
