from datetime import UTC, datetime

from katcha.services.trends import _signal_allowed, _topic_matches_entities
from katcha.trend_models import TrendSignal, TrendTopic

NOW = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)


def _signal(**overrides) -> TrendSignal:
    values = {
        "provider_key": "reddit",
        "external_id": "post-1",
        "observation_key": "obs-1",
        "source_kind": "reddit",
        "independence_key": "r/games",
        "language": "en",
        "region": "us",
        "observed_at": NOW,
        "metrics": {"score": 100.0},
        "media_refs": [],
        "signal_metadata": {},
    }
    values.update(overrides)
    return TrendSignal(**values)


def test_signal_filters_enforce_platform_language_and_region() -> None:
    signal = _signal()

    assert _signal_allowed(
        signal,
        platforms={"reddit"},
        languages={"en"},
        regions={"us"},
    )
    assert not _signal_allowed(
        signal,
        platforms={"youtube"},
        languages={"en"},
        regions={"us"},
    )
    assert not _signal_allowed(
        signal,
        platforms={"reddit"},
        languages={"ja"},
        regions={"us"},
    )
    assert not _signal_allowed(
        signal,
        platforms={"reddit"},
        languages={"en"},
        regions={"gb"},
    )


def test_entity_filter_can_match_topic_or_signal_text() -> None:
    topic = TrendTopic(
        topic_key="project-nova",
        display_name="Project Nova",
        aliases=["Nova"],
        tags=["gaming"],
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    signals = [_signal(title="Project Nova announced for Xbox Series X")]

    assert _topic_matches_entities(topic, signals, ["xbox"])
    assert _topic_matches_entities(topic, signals, ["project nova"])
    assert not _topic_matches_entities(topic, signals, ["playstation"])
