"""Production release health attestation must avoid any secret/config disclosure."""
from __future__ import annotations

import pytest
from fastapi import HTTPException, Response

from katcha.api.main import app, release_identity

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
