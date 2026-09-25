from scripts.ranksnaxx_private_acceptance import _fixture_signals, _publication_payload


def test_fixture_signals_define_five_distinct_acceptance_candidates() -> None:
    rows = [_fixture_signals(index) for index in range(5)]

    assert len(rows) == 5
    assert rows[0]["hook_strength"] == 96
    assert rows[-1]["payoff_strength"] == 100
    assert all(row["source_quality"] == 100 for row in rows)


def test_private_acceptance_publication_cannot_notify_or_publish() -> None:
    payload = _publication_payload("connection-id", "[PRIVATE ACCEPTANCE] test")

    assert payload["privacy_status"] == "private"
    assert payload["publish_at"] is None
    assert payload["notify_subscribers"] is False
    assert payload["contains_synthetic_media"] is True
