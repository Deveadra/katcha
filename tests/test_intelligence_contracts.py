from sqlalchemy import UniqueConstraint

from katcha.api.main import app
from katcha.intelligence_models import RankingSnapshot
from katcha.longform_models import Compilation
from katcha.production_models import Production


def test_editorial_outputs_expose_channel_scope() -> None:
    assert Production.__table__.c.channel_profile_id.nullable is True
    assert Compilation.__table__.c.channel_profile_id.nullable is True


def test_ranking_snapshot_run_key_is_unique_per_channel() -> None:
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in RankingSnapshot.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("channel_profile_id", "run_key") in unique_columns
    assert ("channel_profile_id", "version") in unique_columns


def test_intelligence_control_routes_are_mounted() -> None:
    paths = {route.path for route in app.routes}

    assert "/v1/channels" in paths
    assert "/v1/channels/{channel_profile_id}/intelligence/refresh" in paths
    assert "/v1/channels/{channel_profile_id}/clips/{clip_id}/score" in paths
    assert "/v1/control/events" in paths
    assert "/v1/control/events/{event_id}/ack" in paths
