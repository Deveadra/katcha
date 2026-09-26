import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_runner():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "ranksnaxx_brand_v2_acceptance.py"
    )
    spec = importlib.util.spec_from_file_location(
        "ranksnaxx_brand_v2_acceptance",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _real_ranked_detail() -> dict:
    return {
        "episode": {
            "id": "episode-id",
            "channel_profile_id": "channel-id",
            "trend_opportunity_id": "opportunity-id",
            "render_manifest": {
                "version": "ranked-episode-render-v1",
                "overlays": [
                    {"sequence": 4},
                    {"sequence": 7},
                    {"sequence": 10},
                ],
            },
        },
        "items": [
            {
                "source_snapshot": [
                    {"source_url": f"https://example.com/clip-{index}.mp4"}
                ]
            }
            for index in range(5)
        ],
    }


def test_real_ranked_episode_exposes_narration_sequences() -> None:
    sequences = runner._assert_real_ranked_episode(
        _real_ranked_detail(),
        "channel-id",
    )

    assert sequences == [4, 7, 10]
    assert runner._select_line_ref(sequences, None) == 7
    assert runner._select_line_ref(sequences, 10) == 10


def test_real_ranked_episode_rejects_synthetic_fixture_media() -> None:
    detail = _real_ranked_detail()
    detail["items"][0]["source_snapshot"][0][
        "source_url"
    ] = "http://acceptance-media:8090/clip-1.mp4"

    with pytest.raises(RuntimeError, match="synthetic acceptance fixture"):
        runner._assert_real_ranked_episode(detail, "channel-id")


def test_requested_line_ref_must_exist_in_frozen_narration() -> None:
    with pytest.raises(RuntimeError, match="available narration sequences"):
        runner._select_line_ref([4, 7, 10], 5)


class ExistingStagedBrandClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str):
        assert path == "/v1/channels/channel-id/brands"
        return [
            {
                "brand_key": "ranksnaxx",
                "version": 2,
                "is_active": False,
            },
            {
                "brand_key": "ranksnaxx",
                "version": 1,
                "is_active": True,
            },
        ]

    def post(self, path: str, payload: dict):
        self.posts.append((path, payload))
        raise AssertionError("existing staged Brand v2 must be reused")


def test_existing_staged_v2_is_reused_without_mutation() -> None:
    client = ExistingStagedBrandClient()

    staged = runner._ensure_staged_brand_v2(client, "channel-id")

    assert staged["version"] == 2
    assert staged["is_active"] is False
    assert client.posts == []


class PreviewClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, path: str, payload: dict):
        self.posts.append((path, payload))
        return {
            "id": "preview-id",
            "status": "queued",
            "brand_key": "ranksnaxx",
            "brand_version": 2,
        }


def test_preview_request_uses_real_ranked_episode_and_meme_cry() -> None:
    client = PreviewClient()

    preview = runner._create_preview(
        client,
        "channel-id",
        "episode-id",
        line_ref=7,
        offset_seconds=0.2,
        duration_seconds=1.0,
        anchor="bottom_right",
        animation="pop_bounce",
        scale=0.22,
    )

    assert preview["id"] == "preview-id"
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path.endswith("/brands/2/previews")
    assert payload["short_episode_id"] == "episode-id"
    assert payload["reaction_cue"]["asset_key"] == "meme_cry"
    assert payload["reaction_cue"]["line_ref"] == 7
    assert "/activate" not in path


class ActivationClient:
    def __init__(self, preview: dict) -> None:
        self.preview = preview
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str):
        assert path.endswith("/brand-previews/preview-id")
        return self.preview

    def post(self, path: str, payload: dict):
        self.posts.append((path, payload))
        return {
            "brand_key": "ranksnaxx",
            "version": 2,
            "is_active": True,
        }


def _verified_preview() -> dict:
    return {
        "id": "preview-id",
        "status": "verified",
        "brand_key": "ranksnaxx",
        "brand_version": 2,
        "short_episode_id": "episode-id",
        "verification": {
            "duration_seconds": 17.2,
            "width": 1080,
            "height": 1920,
        },
    }


def test_activation_requires_verified_ranked_preview_evidence() -> None:
    preview = _verified_preview()
    preview["status"] = "rendering"
    client = ActivationClient(preview)

    with pytest.raises(RuntimeError, match="verified preview"):
        runner._activate_reviewed_preview(client, "channel-id", "preview-id")

    assert client.posts == []


def test_verified_preview_activation_is_explicit_separate_action() -> None:
    client = ActivationClient(_verified_preview())

    activated = runner._activate_reviewed_preview(
        client,
        "channel-id",
        "preview-id",
    )

    assert activated["is_active"] is True
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path.endswith("/brands/2/activate")
    assert payload["actor"] == "operator:ranksnaxx-brand-v2-visual-acceptance"


def test_run_refuses_activation_without_visual_confirmation(monkeypatch) -> None:
    class MinimalClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get(self, path: str):
            if path == "/v1/health/ready":
                return {"status": "ok"}
            if path == "/v1/channels/channel-id":
                return {
                    "profile": {
                        "profile_metadata": {"channel_title": "RankSnaxx"}
                    }
                }
            raise AssertionError(path)

    monkeypatch.setattr(runner, "ApiClient", MinimalClient)
    args = SimpleNamespace(
        token="token",
        api_base="http://localhost:8000",
        channel_profile_id="channel-id",
        activate_reviewed_preview="preview-id",
        confirm_visual_acceptance=False,
        episode_id=None,
    )

    with pytest.raises(RuntimeError, match="confirm-visual-acceptance"):
        runner.run(args)
