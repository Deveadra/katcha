from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.trend_models import (
    ChannelTrendWatchVersion,
    TrendEvidencePacket,
    TrendOpportunity,
    TrendSignal,
    TrendTopicSignal,
)


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_trend_signal_snapshots_are_idempotent() -> None:
    assert ("provider_key", "external_id", "observation_key") in _unique_columns(
        TrendSignal.__table__
    )
    assert ("trend_topic_id", "trend_signal_id") in _unique_columns(
        TrendTopicSignal.__table__
    )


def test_watch_profiles_are_versioned_per_channel() -> None:
    assert ("channel_profile_id", "version") in _unique_columns(
        ChannelTrendWatchVersion.__table__
    )
    checks = {
        constraint.name
        for constraint in ChannelTrendWatchVersion.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_trend_watch_min_confidence" in checks
    assert "ck_trend_watch_opportunity_threshold" in checks


def test_opportunities_are_idempotent_per_channel_topic_run() -> None:
    assert ("channel_profile_id", "trend_topic_id", "run_key") in _unique_columns(
        TrendOpportunity.__table__
    )
    assert ("trend_opportunity_id", "version") in _unique_columns(
        TrendEvidencePacket.__table__
    )
