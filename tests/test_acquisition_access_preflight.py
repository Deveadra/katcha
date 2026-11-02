"""Offline, secret-safe preflight for the acquisition control connection."""

from __future__ import annotations

import sys
import uuid

import httpx
import pytest

import katcha.acquisition.worker as worker_module
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


@pytest.mark.parametrize(("status", "expected_reason"), [
    (503, "control_http_503"),
    (502, "control_http_502"),
])
def test_auth_cli_distinguishes_persistent_http_failure_from_transport(
    tmp_path, monkeypatch, caplog, status, expected_reason,
):
    token = tmp_path / "token"
    token.write_text("private-worker-secret", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("KATCHA_ACQUISITION_TOKEN_FILE", str(token))
    monkeypatch.setenv("KATCHA_ACQUISITION_API_URL", "https://app.katcha.stream")
    monkeypatch.setenv("KATCHA_ACQUISITION_CHANNEL_IDS", CHANNEL)
    for name in ("KATCHA_ACQUISITION_CF_ACCESS_ID_FILE",
                 "KATCHA_ACQUISITION_CF_ACCESS_SECRET_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", ["acquisition-worker", "--check-auth"])
    request = httpx.Request("GET", "https://private-worker-secret.invalid/internal")
    response = httpx.Response(
        status, request=request, text="private-worker-secret in response"
    )

    class FakeWorker:
        channels = [CHANNEL]

        def check_authentication(self):
            raise httpx.HTTPStatusError(
                "private-worker-secret", request=request, response=response
            )

        def once(self):
            pytest.fail("Read-only auth preflight must not start job acquisition")

    monkeypatch.setattr(worker_module, "Worker", lambda *args, **kwargs: FakeWorker())
    assert worker_module.main() == 1
    assert expected_reason in caplog.text
    assert "control_transport_error" not in caplog.text
    assert "private-worker-secret" not in caplog.text


def test_auth_cli_success_does_not_run_worker_loop(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("test-private-worker", encoding="utf-8")
    token.chmod(0o600)
    monkeypatch.setenv("KATCHA_ACQUISITION_TOKEN_FILE", str(token))
    monkeypatch.setenv("KATCHA_ACQUISITION_API_URL", "https://app.katcha.stream")
    monkeypatch.setenv("KATCHA_ACQUISITION_CHANNEL_IDS", CHANNEL)
    for name in ("KATCHA_ACQUISITION_CF_ACCESS_ID_FILE",
                 "KATCHA_ACQUISITION_CF_ACCESS_SECRET_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", ["acquisition-worker", "--check-auth"])

    class FakeWorker:
        channels = [CHANNEL]

        def check_authentication(self):
            pass

        def once(self):
            pytest.fail("Read-only auth preflight must not claim any jobs")

    monkeypatch.setattr(worker_module, "Worker", lambda *args, **kwargs: FakeWorker())
    assert worker_module.main() == 0
