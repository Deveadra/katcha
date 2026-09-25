import importlib.util
from pathlib import Path

import pytest


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "ranksnaxx_live_production.py"
    spec = importlib.util.spec_from_file_location("ranksnaxx_live_production", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


class FakeClient:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def get(self, path: str):
        self.paths.append(path)
        if path.startswith("/v1/channels/channel-id/trends/opportunities?"):
            return [
                {"id": "opportunity-1", "opportunity_score": 0.92},
                {"id": "opportunity-2", "opportunity_score": 0.88},
            ]
        if "opportunity-1/activation?" in path:
            return {
                "ready": False,
                "readiness_reason": "insufficient_eligible_clips",
                "eligible": [{"clip_id": "1"}],
                "item_count": 5,
            }
        if "opportunity-2/activation?" in path:
            return {
                "ready": True,
                "readiness_reason": "ready",
                "eligible": [{"clip_id": str(index)} for index in range(5)],
                "item_count": 5,
            }
        raise AssertionError(f"unexpected path: {path}")


def _real_detail() -> dict:
    return {
        "episode": {
            "id": "episode-id",
            "channel_profile_id": "channel-id",
            "trend_opportunity_id": "opportunity-id",
        },
        "items": [
            {
                "acquisition_snapshot": {
                    "eligible": True,
                    "candidate_id": f"candidate-{index}",
                },
                "source_snapshot": [
                    {"source_url": f"https://example.com/clip-{index}.mp4"}
                ],
            }
            for index in range(5)
        ],
    }


def test_private_live_publication_cannot_notify_or_publish() -> None:
    payload = runner._publication_payload("connection-id", "Five wild clips")

    assert payload["privacy_status"] == "private"
    assert payload["publish_at"] is None
    assert payload["notify_subscribers"] is False
    assert payload["contains_synthetic_media"] is False


def test_select_ready_opportunity_skips_non_ready_candidates() -> None:
    client = FakeClient()

    opportunity, preview = runner._select_ready_opportunity(
        client,
        "channel-id",
        item_count=5,
        limit=25,
    )

    assert opportunity["id"] == "opportunity-2"
    assert preview["ready"] is True
    assert any("opportunity-1/activation?" in path for path in client.paths)
    assert any("opportunity-2/activation?" in path for path in client.paths)


def test_real_episode_requires_trend_and_rights_lineage() -> None:
    detail = _real_detail()

    runner._assert_real_episode(detail)

    detail["episode"]["trend_opportunity_id"] = None
    with pytest.raises(RuntimeError, match="real trend opportunity"):
        runner._assert_real_episode(detail)


def test_real_episode_refuses_acceptance_fixture_media() -> None:
    detail = _real_detail()
    detail["items"][0]["source_snapshot"][0][
        "source_url"
    ] = "http://acceptance-media:8090/clip-1.mp4"

    with pytest.raises(RuntimeError, match="synthetic acceptance fixture"):
        runner._assert_real_episode(detail)


def test_existing_publication_is_reused_for_same_episode() -> None:
    class PublicationClient:
        def get(self, path: str):
            assert path == "/v1/publications?limit=250"
            return [
                {"id": "other", "short_episode_id": "different"},
                {
                    "id": "publication-id",
                    "short_episode_id": "episode-id",
                    "privacy_status": "private",
                    "status": "private",
                },
            ]

    publication = runner._existing_episode_publication(
        PublicationClient(),
        "episode-id",
    )

    assert publication is not None
    assert publication["id"] == "publication-id"
