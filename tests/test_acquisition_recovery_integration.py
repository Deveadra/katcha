"""Offline wired proof: real API/auth/DB/worker/upload and independent validation."""

import uuid

import httpx
import pytest
from test_acquisition_handoff import FakeStore, png
from test_acquisition_human_worker import Browser
from test_acquisition_jobs import headers, job_client, job_db  # noqa: F401
from test_acquisition_recovery import recovery_db  # noqa: F401

from katcha import db
from katcha.acquisition import worker as worker_module
from katcha.acquisition.fetch import FetchFailure
from katcha.acquisition.worker import Journal, Worker
from katcha.acquisition_job_models import AcquisitionChallenge
from katcha.services import acquisition_handoff as handoff
from katcha.services import acquisition_jobs as jobs
from katcha.services import acquisition_notifications as notifications


def test_worker_human_notification_retry_and_validated_handoff(
    recovery_db,  # noqa: F811
    job_client,  # noqa: F811
    tmp_path,
    monkeypatch,  # noqa: F811
):
    client, settings, root = job_client
    store = FakeStore()
    monkeypatch.setattr(handoff, "ObjectStore", lambda: store)
    monkeypatch.setattr("katcha.integrations.download.detect_platform", lambda _: "youtube")
    attempts = []
    data = png()

    def extract(
        url, permission, directory, max_bytes, progress, *, cookie_file=None, user_agent=None
    ):
        attempts.append(cookie_file is not None)
        if cookie_file is None:
            raise FetchFailure("authentication_required")
        assert cookie_file.read_text() == "private-session"
        assert user_agent == "fixture-browser-agent"
        path = directory / "media.png"
        path.write_bytes(data)
        return path

    monkeypatch.setattr(worker_module, "fetch_extracted", extract)
    queued = client.post(
        root,
        headers=headers(),
        json={
            "receipt_id": str(recovery_db[2]),
            "media_type": "image",
            "max_bytes": 10000,
        },
    )
    assert queued.status_code == 200, queued.text
    identity = queued.json()["id"]
    path = f"{root}/{identity}"
    browser = Browser()
    worker = Worker(
        "https://control.example",
        "worker-a-fixture-token-001",
        [str(recovery_db[0])],
        tmp_path / "worker",
        client=client,
        storage_client=httpx.Client(transport=httpx.MockTransport(store.client.storage_request)),
        human_browser=browser,
    )
    journal = Journal(tmp_path / "worker", str(recovery_db[0]), identity)
    worker.run_job(journal)
    assert jobs.get_job(recovery_db[0], uuid.UUID(identity)).status == "waiting_human"
    challenge = client.get(path + "/human", headers=headers())
    assert challenge.status_code == 200, challenge.text
    assert challenge.headers["cache-control"] == "no-store"
    assert client.get(path + "/human", headers=headers("worker-a")).status_code == 403
    assert (
        client.post(path + "/human/poll", headers=headers(), json=journal.identity()).status_code
        == 403
    )
    assert (
        client.post(
            path + "/human/poll", headers=headers("worker-b"), json=journal.identity()
        ).status_code
        == 409
    )
    challenge_id = challenge.json()["id"]

    # Real notification outbox, with only external Telegram transport replaced.
    config = settings.model_copy(
        update={
            "telegram_enabled": True,
            "telegram_bot_token": "fixture-token",
            "telegram_acquisition_app_origin": "https://katcha.example",
            "telegram_acquisition_channel_ids": (recovery_db[0],),
        }
    )
    monkeypatch.setattr(notifications, "operator_binding", lambda _: (100, 200))
    sent = []

    class Telegram:
        def send_message(self, chat, text, **kwargs):
            sent.append((chat, text, kwargs))
            return {"message_id": 123}

    assert notifications.deliver_pending_acquisition_notifications(Telegram(), config)["sent"] == 1
    assert notifications.deliver_pending_acquisition_notifications(Telegram(), config)["sent"] == 0
    assert identity in sent[0][2]["reply_markup"]["inline_keyboard"][0][0]["url"]

    command_id = str(uuid.uuid4())
    result = client.post(
        path + "/human/commands",
        headers=headers(),
        json={
            "id": command_id,
            "challenge_id": challenge_id,
            "action": {"action": "open"},
        },
    )
    assert result.status_code == 200, result.text
    worker.run_job(journal)
    result = client.get(path + "/human/commands/" + command_id, headers=headers())
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "completed"
    assert browser.session
    retry = client.post(
        path + "/human/retry", headers=headers(), json={"challenge_id": challenge_id}
    )
    assert retry.status_code == 200, retry.text
    worker.run_job(journal)
    row = jobs.get_job(recovery_db[0], uuid.UUID(identity))
    assert row.status == "awaiting_validation" and not row.media_verified
    assert attempts == [False, True]
    assert not list(journal.directory.glob("*.cookies"))
    accepted = client.post(path + "/validate", headers=headers())
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["media_verified"] and accepted.json()["status"] == "completed"
    worker.run_job(journal)
    assert attempts == [False, True]
    assert client.get(path + "/human", headers=headers()).json()["state"] == "closed"
    with db.session_scope() as session:
        challenge = session.get(AcquisitionChallenge, uuid.UUID(challenge_id))
        assert challenge.notification_state == "sent"


def test_delegated_goal_cannot_use_operator_recovery_controls():
    from fastapi import HTTPException
    from starlette.requests import Request

    from katcha.api.acquisition_jobs import _human_operator

    channel = uuid.uuid4()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.control_scopes = {"*"}
    request.state.control_channel_profile_ids = {channel}
    request.state.control_authentication_mode = "goal_delegation"
    with pytest.raises(HTTPException) as error:
        _human_operator(request, channel)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_temporal_wait_preserves_human_pause_until_verified(monkeypatch):
    from katcha.orchestration import acquisition_workflows as module

    results = iter(["waiting_human", "waiting_human", "leased", "awaiting_validation", "completed"])
    calls = []

    async def execute(name, **kwargs):
        calls.append(kwargs["args"])
        return {"status": next(results)}

    async def sleep(_):
        pass

    monkeypatch.setattr(module, "execute_resource_activity", execute)
    monkeypatch.setattr(module.workflow, "sleep", sleep)
    result = await module.RemoteAcquisitionWorkflow().run("channel", "same-job")
    assert result["status"] == "completed"
    assert calls == [["channel", "same-job"]] * 5
