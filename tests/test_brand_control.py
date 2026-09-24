import uuid

import pytest
from sqlalchemy import Index

from katcha.api.main import app
from katcha.brand_models import ChannelBrandVersion
from katcha.branding import rank_snaxx_brand_v1, rank_snaxx_brand_v2
from katcha.services.channel_brands import brand_contract


def test_channel_brand_model_enforces_single_active_version() -> None:
    indexes = {
        index.name: index
        for index in ChannelBrandVersion.__table__.indexes
        if isinstance(index, Index)
    }
    assert indexes["uq_channel_brand_active"].unique is True


def test_brand_row_identity_must_match_frozen_contract() -> None:
    contract = rank_snaxx_brand_v1()
    row = ChannelBrandVersion(
        channel_profile_id=uuid.uuid4(),
        version=contract.version,
        brand_key=contract.brand_key,
        is_active=True,
        contract=contract.model_dump(mode="json"),
        brand_metadata={},
    )

    assert brand_contract(row) == contract

    row.version = 99
    with pytest.raises(RuntimeError, match="row identity"):
        brand_contract(row)


def test_ranksnaxx_v2_is_explicit_successor_without_mutating_v1() -> None:
    v1 = rank_snaxx_brand_v1()
    v2 = rank_snaxx_brand_v2()

    assert v1.version == 1
    assert "reaction_pack" not in v1.visual
    assert v2.version == 2
    assert v2.visual["reaction_pack"]["pack_key"] == "host_emotes"
    assert v2.experiment_metadata["supersedes_brand_version"] == 1


def test_brand_control_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/channels/{channel_profile_id}/brands" in paths
    assert "/v1/channels/{channel_profile_id}/brand-candidates" in paths
    assert "/v1/channels/{channel_profile_id}/brands/{version}/activate" in paths
