"""Cloudflare Access AI goals must survive worker handoff without broadening auth."""
from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.api.goal_delegation import DelegatedIdentity, GoalDelegatedApp
from katcha.api.main import app
from katcha.config import CloudflareAccessPrincipalSettings, Settings
from katcha.intelligence_models import ChannelProfile
from katcha.services import goal_receipts
from katcha.services.goal_receipts import register_goal, resolve_goal_authority
from katcha.services.goal_tools import run_native_tool


@pytest.fixture
def cf_goal(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(
        db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )
    channel, other = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        for value in (channel, other):
            session.add(
                ChannelProfile(
                    id=value,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
            )
    p = CloudflareAccessPrincipalSettings(
        name="editor",
        email="editor@example.invalid",
        scopes=["ai:read", "ai:command", "channels:read", "ai:write"],
        channel_profile_ids=[str(channel)],
    )
    settings = Settings(
        _env_file=None,
        env="production",
        cloudflare_access_team_domain="https://example.cloudflareaccess.com",
        cloudflare_access_audience="fixture-access-audience",
        cloudflare_access_application_domain="katcha.example.test",
        control_api_token=None,
        control_principals=[],
        cloudflare_access_principals=[p],
        cloudflare_access_team_domain="https://katcha.cloudflareaccess.com",
        cloudflare_access_audience="fixture-access-audience",
        cloudflare_access_application_domain="app.katcha.stream",
    )
    monkeypatch.setattr(goal_receipts, "get_settings", lambda: settings)
    monkeypatch.setattr("katcha.api.control_auth.get_settings", lambda: settings)
    request = {
        "channel_profile_id": str(channel),
        "prompt": "Show me the best clips found today and explain why they scored highly.",
        "thread_id": None,
        "selected_clip_ids": [],
        "resource_refs": [],
        "selected_production_id": None,
    }
    authority = {
        "actor": "control-principal:cloudflare-access:editor",
        "scopes": p.scopes,
        "channel_ids": [str(channel)],
        "principal_name": p.name,
        "credential_id": "cloudflare-access-identity",
        "credential_fingerprint": hashlib.sha256(p.email.encode()).hexdigest()[:12],
    }
    goal = register_goal(uuid.uuid4(), request, authority)
    yield goal, settings, channel, other
    engine.dispose()


def test_cloudflare_goal_revalidates_current_identity_scopes_and_channel(cf_goal):
    goal, settings, channel, other = cf_goal
    scopes, token = resolve_goal_authority(goal)
    assert token is None  # JWT/token must never be retained in a durable goal
    assert {"ai:read", "ai:command"} <= scopes
    assert goal.channel_profile_id == channel
    settings.cloudflare_access_principals[0].channel_profile_ids = [str(other)]
    with pytest.raises(ValueError, match="Channel access"):
        resolve_goal_authority(goal)
    settings.cloudflare_access_principals[0].channel_profile_ids = [str(channel)]
    settings.cloudflare_access_principals[0].scopes = ["ai:read"]
    with pytest.raises(ValueError, match="Command permissions"):
        resolve_goal_authority(goal)
    settings.cloudflare_access_principals[0].scopes = [
        "ai:read", "ai:command", "channels:read", "ai:write"
    ]
    settings.cloudflare_access_principals[0].email = "reassigned@example.invalid"
    with pytest.raises(ValueError, match="identity has changed"):
        resolve_goal_authority(goal)
    settings.cloudflare_access_principals.clear()
    with pytest.raises(ValueError, match="revoked"):
        resolve_goal_authority(goal)


@pytest.mark.asyncio
async def test_cloudflare_goal_native_read_preserves_operator_channel_and_no_jwt(cf_goal):
    goal, settings, channel, other = cf_goal
    # An outside HTTP caller cannot exploit the in-process delegation.
    with TestClient(app) as client:
        unauthenticated = client.get(f"/v1/channels/{channel}/editorial-projects")
    assert unauthenticated.status_code == 401

    # The exact route is authenticated only after goal authority was checked;
    # it cannot use this read to cross into another channel.
    result = await run_native_tool(
        goal, "list_editorial_projects", {"path": {}, "query": {}}, uuid.uuid4()
    )
    assert isinstance(result, dict)
    delegated = GoalDelegatedApp(
        app,
        identity=DelegatedIdentity(
            actor=goal.actor,
            principal_name="editor",
            scopes=frozenset({"ai:read", "ai:command"}),
            channel_id=str(channel),
        ),
        method="GET",
        path=f"/v1/channels/{channel}/editorial-projects",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delegated), base_url="http://native"
    ) as client:
        wrong_channel = await client.get(f"/v1/channels/{other}/editorial-projects")
        wrong_verb = await client.post(f"/v1/channels/{channel}/editorial-projects")
    assert wrong_channel.status_code == 401
    assert wrong_verb.status_code == 401


def test_forged_http_header_cannot_delegate_native_goal(cf_goal):
    _, _, channel, _ = cf_goal
    with TestClient(app) as client:
        response = client.get(
            f"/v1/channels/{channel}/editorial-projects",
            headers={"X-Katcha-Native-Goal-Delegation": "cloudflare-access:editor"},
        )
    assert response.status_code == 401


def test_historic_access_goal_without_email_digest_must_be_resubmitted(cf_goal):
    goal, settings, _, _ = cf_goal
    goal.authority = {
        **goal.authority,
        "credential_fingerprint": None,
        "credential_id": None,
    }
    with pytest.raises(ValueError, match="identity has changed"):
        resolve_goal_authority(goal)
    settings.cloudflare_access_principals.clear()
    with pytest.raises(ValueError, match="revoked"):
        resolve_goal_authority(goal)
