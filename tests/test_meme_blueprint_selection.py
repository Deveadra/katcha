"""New meme recipes must not rewrite saved or explicitly selected channel formats."""
import uuid
from unittest.mock import MagicMock

import pytest

from katcha.edit_blueprint_models import ChannelEditBlueprintVersion
from katcha.editing.blueprints import meme_v1
from katcha.intelligence_models import ChannelProfile
from katcha.services import channel_edit_blueprints as service


def setup_selection(monkeypatch, old=None):
    profile = ChannelProfile(id=uuid.uuid4(), profile_metadata={})
    session = MagicMock()
    monkeypatch.setattr(service, "ensure_active_profile", lambda *_: profile)
    monkeypatch.setattr(service, "_lock_profile", lambda *_: profile)
    monkeypatch.setattr(service, "_active_family", lambda *_: old)
    monkeypatch.setattr(service, "_next_version", lambda *_: 2 if old else 1)
    monkeypatch.setattr(service, "_was_explicitly_activated", lambda *_: False)
    return profile, session


def legacy_row():
    return ChannelEditBlueprintVersion(
        blueprint_key="meme", contract_version="1.0.0", version=1,
        is_active=True, is_default=False, contract=meme_v1().model_dump(mode="json"),
        blueprint_metadata={"created_by": "builtin_selection"},
    )


def test_new_meme_choice_uses_current_builtin_without_changing_default(monkeypatch):
    profile, session = setup_selection(monkeypatch)
    contract, version = service.blueprint_for_channel(session, profile.id, blueprint_key="meme")
    assert contract.version == "2.0.0"
    assert version == 1
    row = session.add.call_args.args[0]
    assert row.channel_profile_id == profile.id
    assert row.is_default is False
    unscoped, unscoped_version = service.blueprint_for_channel(None, None, blueprint_key="meme")
    assert unscoped == contract and unscoped_version is None


def test_upgrade_untouched_meme_keeps_original_contract_and_creates_new_version(monkeypatch):
    old = legacy_row()
    frozen = dict(old.contract)
    profile, session = setup_selection(monkeypatch, old)
    contract, version = service.blueprint_for_channel(session, profile.id, blueprint_key="meme")
    assert contract.version == "2.0.0" and version == 2
    assert old.contract == frozen and old.contract_version == "1.0.0"
    assert old.is_active is False
    replacement = session.add.call_args_list[0].args[0]
    assert replacement.is_active and not replacement.is_default
    assert replacement.blueprint_metadata["supersedes_version"] == 1
    event = session.add.call_args_list[1].args[0]
    assert event.payload["supersedes_version"] == 1
    assert event.payload["contract_version"] == "2.0.0"


@pytest.mark.parametrize(
    "choice", ["customized", "operator", "default", "activation", "event", "profile"],
)
def test_saved_operator_meme_choices_are_preserved(monkeypatch, choice):
    old = legacy_row()
    profile, session = setup_selection(monkeypatch, old)
    if choice == "customized":
        old.contract["ai_guidance"]["operator_notes"] = "Keep my restrained reaction layout."
    elif choice == "operator":
        old.blueprint_metadata = {"created_by": "operator"}
    elif choice == "default":
        old.is_default = True
    elif choice == "activation":
        old.blueprint_metadata = {**old.blueprint_metadata, "explicitly_activated": True}
    elif choice == "event":
        monkeypatch.setattr(service, "_was_explicitly_activated", lambda *_: True)
    elif choice == "profile":
        profile.profile_metadata = {"default_edit_blueprint_key": "meme"}
    contract, version = service.blueprint_for_channel(session, profile.id, blueprint_key="meme")
    assert contract.version == "1.0.0" and version == 1
    assert old.is_active
    session.add.assert_not_called()


def test_upgrade_rechecks_locked_family_and_preserves_concurrent_operator_choice(monkeypatch):
    old = legacy_row()
    chosen = legacy_row()
    chosen.version = 2
    chosen.blueprint_metadata = {"created_by": "operator"}
    profile, session = setup_selection(monkeypatch, old)
    monkeypatch.setattr(service, "_active_family", MagicMock(side_effect=[old, chosen]))
    contract, version = service.blueprint_for_channel(session, profile.id, blueprint_key="meme")
    assert contract.version == "1.0.0" and version == 2
    assert old.is_active and chosen.is_active
    session.add.assert_not_called()


def test_persisted_upgrade_is_idempotent_channel_scoped_and_keeps_snapshots(monkeypatch):
    import test_research_runtime as runtime
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from katcha import db

    engine = create_engine("sqlite://", poolclass=StaticPool)
    db.load_model_metadata()
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    try:
        a_id, _ = runtime.channel()
        b_id, _ = runtime.channel()
        with factory.begin() as session:
            a = session.get(ChannelProfile, a_id)
            b = session.get(ChannelProfile, b_id)
            old_a, old_b = legacy_row(), legacy_row()
            old_a.channel_profile_id, old_b.channel_profile_id = a.id, b.id
            session.add_all([old_a, old_b])
            session.flush()
            frozen = dict(old_a.contract)
            first = service.blueprint_for_channel(session, a.id, blueprint_key="meme")
            second = service.blueprint_for_channel(session, a.id, blueprint_key="meme")
            assert first == second and first[1] == 2
            assert old_a.contract == frozen and old_a.contract_version == "1.0.0"
            assert not old_a.is_active and old_b.is_active
            assert len(list(session.scalars(select(ChannelEditBlueprintVersion)))) == 3
            default = service.ensure_default_edit_blueprint(session, a)
            assert default.blueprint_key == "source_original"
    finally:
        engine.dispose()


def test_upgrade_refreshes_same_row_activation_after_waiting_for_lock(monkeypatch):
    old = legacy_row()
    profile, session = setup_selection(monkeypatch, old)

    def refresh(row):
        if row is old:
            row.blueprint_metadata = {**row.blueprint_metadata, "explicitly_activated": True}

    session.refresh.side_effect = refresh
    contract, version = service.blueprint_for_channel(session, profile.id, blueprint_key="meme")
    assert contract.version == "1.0.0" and version == 1
    assert old.is_active
    session.add.assert_not_called()
