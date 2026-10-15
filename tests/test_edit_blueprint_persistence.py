import uuid

import pytest
from sqlalchemy import Index, UniqueConstraint

from katcha.api.main import app
from katcha.api.schemas import CreateProductionRequest, ProductionResponse
from katcha.api.short_episode_schemas import (
    CreateShortEpisodeRequest,
    ShortEpisodeResponse,
)
from katcha.edit_blueprint_models import ChannelEditBlueprintVersion
from katcha.editing.blueprints import header_explainer_v1, persona_commentary_v1
from katcha.production_models import Production
from katcha.services.channel_edit_blueprints import (
    blueprint_for_channel,
    edit_blueprint_contract,
)
from katcha.short_episode_models import ShortEpisode


def test_edit_blueprint_versions_are_channel_and_family_scoped() -> None:
    unique_sets = {
        tuple(constraint.columns.keys())
        for constraint in ChannelEditBlueprintVersion.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("channel_profile_id", "blueprint_key", "version") in unique_sets

    indexes = {
        index.name: index
        for index in ChannelEditBlueprintVersion.__table__.indexes
        if isinstance(index, Index)
    }
    assert indexes["uq_channel_edit_blueprint_active_family"].unique is True
    assert indexes["uq_channel_edit_blueprint_default"].unique is True


def test_stored_blueprint_contract_must_match_row_identity() -> None:
    contract = persona_commentary_v1()
    row = ChannelEditBlueprintVersion(
        channel_profile_id=uuid.uuid4(),
        blueprint_key=contract.key,
        version=1,
        contract_version=contract.version,
        is_active=True,
        is_default=True,
        contract=contract.model_dump(mode="json"),
        blueprint_metadata={},
    )

    restored = edit_blueprint_contract(row)
    assert restored == contract

    row.blueprint_key = "different-channel-format"
    with pytest.raises(RuntimeError, match="row key"):
        edit_blueprint_contract(row)


def test_unscoped_legacy_production_uses_safe_builtin_blueprint() -> None:
    contract, channel_version = blueprint_for_channel(
        None,
        None,
        blueprint_key=None,
    )

    assert contract.key == "source_original"
    assert channel_version is None


def test_blueprint_control_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/channels/edit-blueprint-templates" in paths
    assert "/v1/channels/{channel_profile_id}/edit-blueprints" in paths
    assert (
        "/v1/channels/{channel_profile_id}/edit-blueprints/"
        "{blueprint_key}/{version}/activate"
    ) in paths


def test_production_and_episode_models_freeze_editing_lineage() -> None:
    production_columns = Production.__table__.c
    episode_columns = ShortEpisode.__table__.c

    for columns in (production_columns, episode_columns):
        assert columns.edit_blueprint_key.nullable is True
        assert columns.edit_blueprint_version.nullable is True
        assert columns.edit_blueprint_snapshot.nullable is True


def test_creation_and_response_contracts_expose_blueprint_selection() -> None:
    assert "edit_blueprint_key" in CreateProductionRequest.model_fields
    assert "edit_blueprint_key" in CreateShortEpisodeRequest.model_fields
    assert {
        "edit_blueprint_key",
        "edit_blueprint_version",
        "edit_blueprint_snapshot",
    } <= set(ProductionResponse.model_fields)
    assert {
        "edit_blueprint_key",
        "edit_blueprint_version",
        "edit_blueprint_snapshot",
    } <= set(ShortEpisodeResponse.model_fields)


def test_built_in_blueprints_remain_distinct_after_persistence_round_trip() -> None:
    persona = persona_commentary_v1()
    header = header_explainer_v1()

    persona_row = ChannelEditBlueprintVersion(
        channel_profile_id=uuid.uuid4(),
        blueprint_key=persona.key,
        version=1,
        contract_version=persona.version,
        is_active=True,
        is_default=True,
        contract=persona.model_dump(mode="json"),
        blueprint_metadata={},
    )
    header_row = ChannelEditBlueprintVersion(
        channel_profile_id=uuid.uuid4(),
        blueprint_key=header.key,
        version=1,
        contract_version=header.version,
        is_active=True,
        is_default=True,
        contract=header.model_dump(mode="json"),
        blueprint_metadata={},
    )

    assert edit_blueprint_contract(persona_row).narration.mode == "persona_voice"
    assert edit_blueprint_contract(header_row).source_layout.mode == "header_panel"


def test_untouched_bootstrap_default_moves_to_source_without_rewriting_history(monkeypatch):
    from unittest.mock import MagicMock

    from katcha.intelligence_models import ChannelProfile
    from katcha.services import channel_edit_blueprints as service

    profile = ChannelProfile(id=uuid.uuid4(), profile_metadata={})
    original = persona_commentary_v1().model_dump(mode="json")
    old = ChannelEditBlueprintVersion(
        blueprint_key="persona_commentary", contract_version="1.0.0",
        version=1, is_active=True, is_default=True, contract=original,
        blueprint_metadata={"created_by": "profile_bootstrap"},
    )
    session = MagicMock()
    monkeypatch.setattr(service, "_lock_profile", lambda *_: profile)
    monkeypatch.setattr(service, "_default_row", lambda *_: old)
    monkeypatch.setattr(service, "_active_family", lambda *_: None)
    monkeypatch.setattr(service, "_next_version", lambda *_: 1)
    monkeypatch.setattr(service, "_was_explicitly_activated", lambda *_: False)

    new = service.ensure_default_edit_blueprint(session, profile)

    assert new.blueprint_key == "source_original"
    assert new.is_default
    assert not old.is_default
    assert old.is_active
    assert old.contract == original


@pytest.mark.parametrize("choice", ["profile", "activation", "customized", "historical_activation"])
def test_explicit_channel_blueprint_choices_are_preserved(monkeypatch, choice):
    from unittest.mock import MagicMock

    from katcha.intelligence_models import ChannelProfile
    from katcha.services import channel_edit_blueprints as service

    profile = ChannelProfile(id=uuid.uuid4(), profile_metadata={})
    contract = persona_commentary_v1().model_dump(mode="json")
    metadata = {"created_by": "profile_bootstrap"}
    if choice == "profile":
        profile.profile_metadata = {"default_edit_blueprint_key": "persona_commentary"}
    elif choice == "activation":
        metadata["explicitly_activated"] = True
    elif choice == "customized":
        contract["ai_guidance"]["operator_notes"] = "Use my custom voice pacing."
    old = ChannelEditBlueprintVersion(
        blueprint_key="persona_commentary", contract_version="1.0.0",
        version=1, is_active=True, is_default=True, contract=contract,
        blueprint_metadata=metadata,
    )
    session = MagicMock()
    monkeypatch.setattr(service, "_lock_profile", lambda *_: profile)
    monkeypatch.setattr(service, "_default_row", lambda *_: old)

    monkeypatch.setattr(
        service, "_was_explicitly_activated", lambda *_: choice == "historical_activation",
    )
    assert service.ensure_default_edit_blueprint(session, profile) is old
    assert old.is_default
    session.add.assert_not_called()


def test_explicit_builtin_selection_is_available_without_prior_channel_setup(monkeypatch):
    from unittest.mock import MagicMock

    from katcha.intelligence_models import ChannelProfile
    from katcha.services import channel_edit_blueprints as service

    profile = ChannelProfile(id=uuid.uuid4(), profile_metadata={})
    session = MagicMock()
    monkeypatch.setattr(service, "ensure_active_profile", lambda *_: profile)
    monkeypatch.setattr(service, "_lock_profile", lambda *_: profile)
    monkeypatch.setattr(service, "_active_family", lambda *_: None)
    monkeypatch.setattr(service, "_next_version", lambda *_: 1)

    contract, version = service.blueprint_for_channel(
        session, profile.id, blueprint_key="source_original",
    )

    assert contract.key == "source_original"
    assert version == 1
    persisted = session.add.call_args.args[0]
    assert persisted.channel_profile_id == profile.id
    assert not persisted.is_default


def test_blueprint_templates_offer_original_and_meme_without_commentary():
    from katcha.api.edit_blueprints import get_edit_blueprint_templates

    templates = get_edit_blueprint_templates()
    assert templates[0].key == "source_original"
    by_key = {row.key: row.contract for row in templates}
    assert by_key["source_original"]["narration"]["mode"] == "source_only"
    assert by_key["meme"]["meme"]["enabled"] is True
    assert by_key["persona_commentary"]["narration"]["required"] is True
