"""Command media acquisition must not spend every bounded attempt on one host."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate, DiscoveryObservation, DiscoveryRun
from katcha.orchestration import discovery_activities as activity

saved = saved_fixture


def row(number, host="www.youtube.com", title="VisionQuest official trailer", platform="youtube"):
    candidate_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"diversity-fixture-{number}")
    return (
        candidate_id,
        title,
        {"description": "A public media source"},
        platform,
        f"https://{host}/watch?v=fixture-{number}",
    )


def test_diverse_hosts_consume_distinct_slots_before_repeating_provider():
    candidates = [
        row(1), row(2, host="youtu.be"), row(3, host="m.youtube.com"),
        row(4), row(5), row(6, host="press.example.org", platform="web"),
        row(7, host="media.example.net", platform="web"),
    ]
    selected, detail = activity.select_command_media_candidates(
        candidates, ["visionquest"], trailers_only=False, max_candidates=5
    )
    assert [candidate_id for candidate_id, _ in selected] == [
        candidates[i][0] for i in (0, 5, 6, 1, 2)
    ]
    assert [route for _, route in selected] == [
        "youtube", "press.example.org", "media.example.net", "youtube", "youtube"
    ]
    assert detail == {
        "matching_candidate_count": 7,
        "selected_candidate_count": 5,
        "distinct_selected_routes": 3,
        "diversified": True,
    }


def test_only_one_host_retains_original_order_and_attempt_limit():
    candidates = [row(i) for i in range(12)]
    selected, detail = activity.select_command_media_candidates(
        candidates, [], trailers_only=False, max_candidates=5
    )
    assert [candidate_id for candidate_id, _ in selected] == [
        candidate[0] for candidate in candidates[:5]
    ]
    assert detail["diversified"] is False
    assert detail["matching_candidate_count"] == 12
    assert detail["distinct_selected_routes"] == 1


def test_topic_and_trailer_filter_applies_before_diversification():
    candidates = [
        row(1, title="Unrelated official trailer"),
        row(2, title="VisionQuest behind the scenes"),
        row(3, host="press.example.org", title="VisionQuest official trailer", platform="web"),
        row(4, host="media.example.net", title="VisionQuest teaser", platform="web"),
    ]
    selected, detail = activity.select_command_media_candidates(
        candidates, ["visionquest"], trailers_only=True, max_candidates=2
    )
    assert [candidate_id for candidate_id, _ in selected] == [
        candidates[2][0], candidates[3][0]
    ]
    assert detail["matching_candidate_count"] == 2


def test_host_is_authoritative_not_an_untrusted_platform_label():
    candidates = [
        row(1, host="press.example.org", platform="youtube"),
        row(2, host="press2.example.org", platform="youtube"),
        row(3, host="www.youtube.com", platform="web"),
        row(4, host="youtube.com.evil.example", platform="youtube"),
    ]
    selected, detail = activity.select_command_media_candidates(
        candidates, [], trailers_only=False, max_candidates=4
    )
    assert [route for _, route in selected] == [
        "press.example.org", "press2.example.org", "youtube",
        "youtube.com.evil.example",
    ]
    assert detail["distinct_selected_routes"] == 4


def test_bounded_selection_with_malformed_or_duplicate_candidates():
    duplicate = row(1)
    candidates = [
        duplicate, duplicate,
        (uuid.uuid4(), "Clip", {}, "web", "http://[invalid"),
        (uuid.uuid4(), "Clip", {}, "web", "no-host"),
    ]
    selected, detail = activity.select_command_media_candidates(
        candidates, [], trailers_only=False, max_candidates=9999
    )
    assert len(selected) == 3
    assert detail["matching_candidate_count"] == 3
    assert set(route for _, route in selected) == {"youtube", "platform:web"}


def test_activity_reaches_non_youtube_candidate_and_keeps_existing_promotion_gate(
    saved, monkeypatch
):
    run_id = uuid.uuid4()
    now = datetime.now(UTC)
    with db.session_scope() as session:
        session.add(DiscoveryRun(
            id=run_id, adapter_key="youtube", adapter_version="v1",
            run_key=str(run_id),
            run_metadata={
                "command_prepare_for_production": True,
                "command_prepare_max_candidates": 3,
                "command_match_terms": ["visionquest"],
            },
        ))
        for index, source in enumerate([
            row(101), row(102), row(103), row(104), row(105),
            row(106, host="press.example.org", platform="web"),
        ]):
            candidate_id, title, metadata, platform, url = source
            session.add(DiscoveryCandidate(
                id=candidate_id, adapter_key="fixture", external_id=str(index),
                source_url=url, canonical_url=url, platform=platform,
                title=title, candidate_metadata=metadata,
            ))
            session.add(DiscoveryObservation(
                id=uuid.uuid4(), discovery_run_id=run_id,
                discovery_candidate_id=candidate_id,
                observed_at=now + timedelta(seconds=index),
            ))

    attempts = []
    def promote(candidate_id, *, actor, for_review):
        assert actor == "katcha-ai" and for_review is True
        attempts.append(candidate_id)
        if candidate_id == row(106, host="press.example.org", platform="web")[0]:
            raise ValueError("Media source is unavailable for preparation")
        return SimpleNamespace(
            id=uuid.uuid5(uuid.NAMESPACE_DNS, str(candidate_id)),
            status="queued", error=None, workflow_id=f"ingest-{candidate_id}",
            clip_id=None,
        )

    monkeypatch.setattr(activity, "promote_discovery_candidate", promote)
    result = activity.prepare_command_discovery_candidates_activity(str(run_id))
    assert attempts == [row(101)[0], row(106)[0], row(102)[0]]
    assert len(result["prepared"]) == 2
    assert result["errors"][0]["candidate_id"] == str(row(106)[0])
    assert result["errors"][0]["source_route"] == "press.example.org"
    assert result["errors"][0]["error"] == "Media source is unavailable for preparation"
    assert result["selection"]["matching_candidate_count"] == 6
    assert result["selection"]["distinct_selected_routes"] == 2
    assert result["selection"]["diversified"] is True
    assert all(item.get("clip_id") is None for item in result["prepared"])
