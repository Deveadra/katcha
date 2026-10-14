"""Production release health attestation must avoid any secret/config disclosure."""
from __future__ import annotations

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient

from katcha.api import control_auth
from katcha.api.main import app, release_identity
from katcha.config import Settings

SHA = "e" * 40


def test_release_route_is_public_and_stable() -> None:
    assert any(getattr(route, "path", None) == "/v1/health/release" for route in app.routes)


def test_release_health_reports_exact_configured_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KATCHA_RELEASE_SHA", SHA)
    assert release_identity(Response()) == {"release_sha": SHA}
    response = Response()
    release_identity(response)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("bad", ["", "main", "f" * 39, "f" * 41, "F" * 40])
def test_release_health_fails_closed_on_noncommit(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("KATCHA_RELEASE_SHA", bad)
    with pytest.raises(HTTPException) as caught:
        release_identity(Response())
    assert caught.value.status_code == 503
    assert "release identity unavailable" in str(caught.value.detail)


def test_release_health_does_not_return_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KATCHA_RELEASE_SHA", SHA)
    monkeypatch.setenv("KATCHA_CONTROL_API_TOKEN", "private-token-value")
    monkeypatch.setenv("KATCHA_CLOUDFLARE_TUNNEL_TOKEN", "private-tunnel-value")
    returned = release_identity(Response())
    assert returned == {"release_sha": SHA}
    assert "private" not in str(returned)


@pytest.mark.parametrize("release_sha", [SHA, "", "main"])
def test_public_release_http_preserves_control_auth(
    monkeypatch: pytest.MonkeyPatch, release_sha: str
) -> None:
    settings = Settings(
        _env_file=None, control_api_token="release-http-fixture-token-000001"
    )
    monkeypatch.setattr(control_auth, "get_settings", lambda: settings)
    monkeypatch.setenv("KATCHA_RELEASE_SHA", release_sha)
    client = TestClient(app)
    response = client.get("/v1/health/release")
    if release_sha == SHA:
        assert response.status_code == 200
        assert response.json() == {"release_sha": SHA}
        assert response.headers["cache-control"] == "no-store"
    else:
        assert response.status_code == 503
        assert response.json() == {"detail": "release identity unavailable"}
    assert client.get("/v1/runtime/ai").status_code == 401
    assert "release-http-fixture-token" not in response.text
