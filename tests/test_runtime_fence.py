from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from katcha.runtime_fence import LeadershipFenceError, assert_mutation_authority


def _settings(**overrides):
    values = {
        "leadership_fence_mode": "http",
        "leadership_fence_url": "https://fence.katcha.test/v1/fence/assert",
        "leadership_fence_token": SecretStr("fence-token-with-enough-entropy"),
        "leadership_fence_timeout_seconds": 3.0,
        "deployment_id": "oci-primary-a1",
        "deployment_epoch": 7,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_disabled_fence_skips_network(monkeypatch) -> None:
    def fail_post(*_args, **_kwargs):
        raise AssertionError("disabled fencing must not call the coordinator")

    monkeypatch.setattr(httpx, "post", fail_post)
    assert (
        assert_mutation_authority(
            "storage.put",
            settings=_settings(leadership_fence_mode="disabled"),
        )
        is None
    )


def test_http_fence_accepts_exact_active_identity(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):
        assert url == "https://fence.katcha.test/v1/fence/assert"
        assert headers["Authorization"].startswith("Bearer ")
        assert json == {
            "deployment_id": "oci-primary-a1",
            "deployment_epoch": 7,
            "operation": "youtube.upload_chunk",
        }
        assert timeout == 3.0
        return httpx.Response(
            200,
            json={
                "authorized": True,
                "active_epoch": 7,
                "leader_id": "oci-primary-a1",
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    receipt = assert_mutation_authority(
        "youtube.upload_chunk",
        settings=_settings(),
    )
    assert receipt is not None
    assert receipt.active_epoch == 7
    assert receipt.leader_id == "oci-primary-a1"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"authorized": False, "active_epoch": 8, "leader_id": "oci-paid-fallback"},
            "runtime is fenced",
        ),
        (
            {"authorized": True, "active_epoch": 8, "leader_id": "oci-primary-a1"},
            "runtime is fenced",
        ),
        (
            {"authorized": True, "active_epoch": 7, "leader_id": "other-host"},
            "runtime is fenced",
        ),
    ],
)
def test_http_fence_rejects_stale_or_nonleader_runtime(
    monkeypatch,
    payload,
    message,
) -> None:
    def fake_post(url, **_kwargs):
        return httpx.Response(
            200,
            json=payload,
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(LeadershipFenceError, match=message):
        assert_mutation_authority("youtube.publish", settings=_settings())


def test_http_fence_fails_closed_when_coordinator_is_unavailable(monkeypatch) -> None:
    def fail_post(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", fail_post)
    with pytest.raises(LeadershipFenceError, match="coordinator unavailable"):
        assert_mutation_authority("render.dispatch", settings=_settings())


def test_http_fence_fails_closed_when_identity_is_incomplete() -> None:
    with pytest.raises(LeadershipFenceError, match="identity is incomplete"):
        assert_mutation_authority(
            "storage.put",
            settings=_settings(deployment_epoch=0),
        )
