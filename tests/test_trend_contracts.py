from sqlalchemy import CheckConstraint, UniqueConstraint

from katcha.acquisition_models import CandidateTrendScore, TopicWatchVersion
from katcha.api.main import app
from katcha.api.trends import _contains_secret_key


def _unique_columns(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_topic_watch_versions_are_immutable_and_bounded() -> None:
    assert ("watch_key", "version") in _unique_columns(TopicWatchVersion.__table__)
    checks = {
        constraint.name
        for constraint in TopicWatchVersion.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_topic_watch_version_positive" in checks
    assert "ck_topic_watch_freshness_positive" in checks
    assert "ck_topic_watch_max_candidates_positive" in checks


def test_trend_scores_have_version_and_retry_idempotency_keys() -> None:
    unique = _unique_columns(CandidateTrendScore.__table__)

    assert ("topic_watch_id", "discovery_candidate_id", "version") in unique
    assert ("topic_watch_id", "discovery_candidate_id", "score_key") in unique


def test_topic_watch_config_rejects_secret_bearing_shapes() -> None:
    assert _contains_secret_key({"query": {"api_key": "nope"}}) is True
    assert _contains_secret_key({"headers": {"Authorization": "nope"}}) is True
    assert _contains_secret_key({"query": {"feed_url": "https://example.com"}}) is False


def test_trend_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])

    assert "/v1/trends/watches" in paths
    assert "/v1/trends/watches/{topic_watch_id}/execute" in paths
    assert (
        "/v1/trends/watches/{topic_watch_id}/candidates/{candidate_id}/score"
        in paths
    )
    assert "/v1/trends/watches/{topic_watch_id}/ranked" in paths
