from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from katcha.config import Settings
from katcha.integrations import storage as storage_module
from katcha.integrations.youtube import client as youtube_module
from katcha.rendering import client as rendering_module
from katcha.runtime_fence import LeadershipFenceError


def _deny(*_args, **_kwargs):
    raise LeadershipFenceError("stale runtime")


def test_youtube_upload_is_fenced_before_remote_transport(monkeypatch) -> None:
    monkeypatch.setattr(youtube_module, "assert_mutation_authority", _deny)

    def fail_http(*_args, **_kwargs):
        raise AssertionError("YouTube transport must not run after a fence denial")

    monkeypatch.setattr(youtube_module.httpx, "post", fail_http)
    client = youtube_module.YouTubeClient(uuid.uuid4(), settings=Settings())

    with pytest.raises(LeadershipFenceError, match="stale runtime"):
        client.initiate_resumable_upload(
            title="test",
            description="",
            tags=[],
            category_id="24",
            size_bytes=10,
            mime_type="video/mp4",
            notify_subscribers=False,
            made_for_kids=False,
            contains_synthetic_media=False,
        )


def test_object_store_write_is_fenced_before_remote_transport(monkeypatch) -> None:
    monkeypatch.setattr(storage_module, "assert_mutation_authority", _deny)

    class FailClient:
        def put_object(self, **_kwargs):
            raise AssertionError("object-store transport must not run after a fence denial")

    store = object.__new__(storage_module.ObjectStore)
    store.settings = Settings()
    store.client = FailClient()

    with pytest.raises(LeadershipFenceError, match="stale runtime"):
        store.put_bytes(b"test", "fenced/test.txt", content_type="text/plain")


def test_render_dispatch_is_fenced_before_renderer_transport(monkeypatch) -> None:
    monkeypatch.setattr(rendering_module, "assert_mutation_authority", _deny)

    class FailClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("renderer transport must not run after a fence denial")

    monkeypatch.setattr(rendering_module.httpx, "Client", FailClient)
    manifest = SimpleNamespace(output_duration_seconds=1.0)

    with pytest.raises(LeadershipFenceError, match="stale runtime"):
        rendering_module._render(manifest, settings=Settings())


def test_chatgpt_plan_inference_is_fenced_before_stream_transport(monkeypatch) -> None:
    from katcha.integrations import chatgpt

    monkeypatch.setattr(chatgpt, "assert_mutation_authority", _deny)

    def fail_stream(*_args, **_kwargs):
        raise AssertionError("ChatGPT stream must not run after a fence denial")

    monkeypatch.setattr(chatgpt.httpx, "stream", fail_stream)
    session = chatgpt.ChatGPTSession(
        connection_id=uuid.uuid4(),
        access_token="secret",
        model="test-model",
        email=None,
        display_name=None,
    )
    with pytest.raises(LeadershipFenceError, match="stale runtime"):
        chatgpt._stream_plan_response(
            session,
            body={"input": []},
            timeout=1.0,
        )
