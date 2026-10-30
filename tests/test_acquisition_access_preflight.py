"""Offline, secret-safe preflight for the acquisition control connection."""

from __future__ import annotations

import uuid

import httpx
import pytest

from katcha.acquisition.worker import LeaseLost, Worker

CHANNEL = str(uuid.UUID("aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"))
ACCESS = {
    "CF-Access-Client-Id": "fixture-client-id",
    "CF-Access-Client-Secret": "fixture-client-secret",
}


def build_worker(tmp_path, handler):
    return Worker(
        "https://app.katcha.stream",
        "fixture-worker-bearer",
        [CHANNEL],
        tmp_path / "worker",
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        access_headers=ACCESS,
    )


def test_preflight_requires_real_katcha_health_and_channel_authorization(tmp_path):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["CF-Access-Client-Id"] == ACCESS["CF-Access-Client-Id"]
        assert request.headers["CF-Access-Client-Secret"] == ACCESS["CF-Access-Client-Secret"]
        assert request.headers["Authorization"] == "Bearer fixture-worker-bearer"
        if request.url.path == "/v1/health/live":
            return httpx.Response(200, json={"status": "ok", "service": "katcha"})
        assert request.url.path == f"/v1/channels/{CHANNEL}/acquisition-jobs"
        assert request.url.params["status"] == "queued"
        assert request.url.params["limit"] == "1"
        return httpx.Response(200, json=[])

    build_worker(tmp_path, handle).check_authentication()
    assert paths == ["/v1/health/live", f"/v1/channels/{CHANNEL}/acquisition-jobs"]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            httpx.Response(
                403, headers={"content-type": "text/html"}, text="cloudflare-secret-sentinel"
            ),
            "edge_or_unclassified_http_403",
        ),
        (
            httpx.Response(403, json={"detail": "private-worker-secret-sentinel"}),
            "katcha_principal_http_403",
        ),
        (
            httpx.Response(401, json={"detail": "expired-private-sentinel"}),
            "katcha_principal_http_401",
        ),
        (
            httpx.Response(
                200, headers={"content-type": "text/html"}, text="secret-html-sentinel"
            ),
            "control_non_json_response",
        ),
        (
            httpx.Response(
                302, headers={"Location": "https://login.example/secret-token-sentinel"}
            ),
            "edge_or_unclassified_redirect",
        ),
    ],
)
def test_preflight_denies_and_redacts_failed_control_responses(tmp_path, response, reason):
    def handle(_request):
        return response

    worker = build_worker(tmp_path, handle)
    with pytest.raises(LeaseLost) as failure:
        worker.check_authentication()
    assert str(failure.value) == reason
    assert "secret" not in str(failure.value)
    assert "token" not in str(failure.value)


def test_preflight_rejects_proxy_health_success_that_is_not_katcha(tmp_path):
    worker = build_worker(
        tmp_path,
        lambda _: httpx.Response(200, json={"status": "ok", "service": "intermediary"}),
    )
    with pytest.raises(LeaseLost, match="control_health_unexpected_response"):
        worker.check_authentication()


def test_preflight_rejects_unexpected_authenticated_payload(tmp_path):
    def handle(request):
        if request.url.path == "/v1/health/live":
            return httpx.Response(200, json={"status": "ok", "service": "katcha"})
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(LeaseLost, match="worker_principal_unexpected_response"):
        build_worker(tmp_path, handle).check_authentication()
