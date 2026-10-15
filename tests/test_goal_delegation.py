"""Control-plane delegation is in-process only and never header-derived."""

from fastapi.testclient import TestClient

from katcha.api.goal_delegation import (
    DelegatedIdentity,
    GoalDelegatedApp,
    delegated_identity,
)
from katcha.api.main import app
from katcha.config import Settings


def test_fake_external_header_cannot_delegate_access(monkeypatch):
    # A production configuration with only Cloudflare Access operators must
    # reject direct unauthenticated calls, even with guessed delegation headers.
    settings = Settings(
        _env_file=None,
        env="production",
        cloudflare_access_team_domain="https://example.cloudflareaccess.com",
        cloudflare_access_audience="fixture-access-audience",
        cloudflare_access_application_domain="katcha.example.test",
        control_api_token=None,
        control_principals=[],
        cloudflare_access_team_domain="https://katcha.cloudflareaccess.com",
        cloudflare_access_audience="fixture-access-audience",
        cloudflare_access_application_domain="app.katcha.stream",
        cloudflare_access_principals=[
            {
                "name": "forescene",
                "email": "editor@example.test",
                "scopes": ["ai:read", "channels:read"],
                "channel_profile_ids": ["11111111-1111-4111-8111-111111111111"],
            }
        ],
    )
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    identity = DelegatedIdentity(
        actor="control-principal:cloudflare-access:forescene",
        principal_name="forescene",
        scopes=frozenset({"ai:read", "channels:read"}),
        channel_id="11111111-1111-4111-8111-111111111111",
        credential_id="cloudflare-access-identity",
        credential_fingerprint="example-email-binding",
    )
    assert delegated_identity({"_katcha_native_goal_delegation": identity}) is None
    assert delegated_identity({
        "_katcha_native_goal_delegation": (object(), identity)
    }) is None

    with TestClient(app) as client:
        denied = client.get(
            "/v1/control/session",
            headers={"X-Katcha-Native-Goal-Delegation": "forescene"},
        )
        assert denied.status_code == 401

    with TestClient(
        GoalDelegatedApp(
            app, identity=identity, method="GET", path="/v1/control/session"
        )
    ) as client:
        allowed = client.get("/v1/control/session")
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["actor"] == identity.actor
        assert allowed.json()["principal_name"] == identity.principal_name
        # Delegation cannot bleed into a different route, method or channel.
        assert client.get("/v1/channels").status_code == 401
        assert client.post("/v1/control/session").status_code == 401
        assert client.get(
            "/v1/channels/22222222-2222-4222-8222-222222222222"
        ).status_code == 401
