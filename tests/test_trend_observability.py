from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from katcha.api.main import app
from katcha.services.trends import (
    _material_confidence_change,
    _rights_readiness,
)
from katcha.trends.scoring import (
    SignalSample,
    TopicDescriptor,
    WatchConfig,
    score_topic,
)


def _signal(*statuses: str) -> SimpleNamespace:
    return SimpleNamespace(
        media_refs=[{"rights_status": status} for status in statuses]
    )


def test_raw_signal_path_supports_bounded_read_and_ingest_methods() -> None:
    contract = app.openapi()["paths"]["/v1/trends/signals"]
    assert set(contract) >= {"get", "post"}
    params = {
        item["name"]: item
        for item in contract["get"]["parameters"]
    }
    assert {"provider_key", "source_kind", "language", "region", "topic_id", "before", "limit"} <= set(params)
    limit_schema = params["limit"]["schema"]
    assert limit_schema["maximum"] == 250
    assert limit_schema["minimum"] == 1


def test_rights_readiness_is_conservative_and_explicit_only() -> None:
    assert _rights_readiness([]) == 0.0
    assert _rights_readiness([_signal("unassessed")]) == 0.0
    assert _rights_readiness([_signal("blocked")]) == 0.0
    assert _rights_readiness([_signal("review_required")]) == 0.5
    assert _rights_readiness([_signal("owned")]) == 1.0
    assert _rights_readiness([_signal("owned", "unassessed")]) == 0.5


def test_material_confidence_change_uses_ten_point_threshold() -> None:
    assert not _material_confidence_change(0.50, 0.599999)
    assert _material_confidence_change(0.50, 0.60)
    assert _material_confidence_change(0.70, 0.59)


def test_popularity_score_has_no_rights_input_or_component() -> None:
    now = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
    result = score_topic(
        topic=TopicDescriptor("Project Nova"),
        samples=[
            SignalSample(
                entity_key="youtube:abc",
                source_kind="youtube",
                independence_key="youtube:channel-a",
                observed_at=now,
                published_at=now,
                metrics={"views": 1000, "comments": 50},
            )
        ],
        watch=WatchConfig(interests=("Project Nova",)),
        now=now,
    )
    assert "rights_readiness" not in result.components
