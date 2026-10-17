"""Channel-scoped control actions cannot stop unrelated work."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from katcha.api.clip_library import (
    ClipWorkStopRequest,
    stop_clip_blocking_work,
)
from katcha.services import clip_work_resolution as resolution
from test_clip_dependencies import fixture_work


def operator_request(channel_id, *, scopes=None, allowed=None):
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/clips/test/references/production/test/stop",
        "headers": [],
        "state": {
            "control_actor": "control-principal:test",
            "control_scopes": set(scopes or {"production:create"}),
            "control_channel_profile_ids": set(allowed or {str(channel_id)}),
            "control_principal_name": "test",
        },
    })


@pytest.mark.asyncio
async def test_stop_is_confirmed_and_bound_to_resource_channel(
    fixture_work, monkeypatch,
):
    clip_id, channel_id, other_id, ids = fixture_work
    payload = ClipWorkStopRequest(
        expected_status="review", confirmation_text=f"STOP {ids['production']}"
    )
    calls = []

    async def fake_stop(*args, **kwargs):
        calls.append((args, kwargs))
        return resolution.WorkStopResult(
            "production", ids["production"], "cancellation_requested",
            "review", "Cancellation requested",
        )

    monkeypatch.setattr("katcha.api.clip_library.stop_clip_work", fake_stop)
    with pytest.raises(HTTPException) as wrong_confirmation:
        await stop_clip_blocking_work(
            clip_id, "production", ids["production"],
            ClipWorkStopRequest(expected_status="review", confirmation_text="STOP wrong"),
            operator_request(channel_id),
        )
    assert wrong_confirmation.value.status_code == 400

    with pytest.raises(HTTPException) as wrong_channel:
        await stop_clip_blocking_work(
            clip_id, "production", ids["production"], payload, operator_request(other_id),
        )
    assert wrong_channel.value.status_code == 403

    with pytest.raises(HTTPException) as weak_scope:
        await stop_clip_blocking_work(
            clip_id, "production", ids["production"], payload,
            operator_request(channel_id, scopes={"channels:read"}),
        )
    assert weak_scope.value.status_code == 403

    result = await stop_clip_blocking_work(
        clip_id, "production", ids["production"], payload,
        operator_request(channel_id),
    )
    assert result.state == "cancellation_requested"
    assert len(calls) == 1
    assert calls[0][0] == (clip_id, "production", ids["production"])
    assert calls[0][1]["actor"] == "control-principal:test"


@pytest.mark.asyncio
async def test_stop_does_not_accept_unrelated_clip(fixture_work, monkeypatch):
    clip_id, channel_id, _, ids = fixture_work
    async def forbidden(*args, **kwargs):
        raise AssertionError("no Temporal call for unrelated clip")

    monkeypatch.setattr("katcha.api.clip_library.stop_clip_work", forbidden)
    with pytest.raises(HTTPException) as caught:
        await stop_clip_blocking_work(
            uuid.uuid4(), "production", ids["production"],
            ClipWorkStopRequest(
                expected_status="review", confirmation_text=f"STOP {ids['production']}"
            ),
            operator_request(channel_id),
        )
    assert caught.value.status_code == 404


def test_route_is_visible_to_clients():
    from katcha.api.main import app

    path = "/v1/clips/{clip_id}/references/{kind}/{work_id}/stop"
    assert "post" in app.openapi()["paths"][path]
