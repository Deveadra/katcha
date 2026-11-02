"""Offline worker recovery: explicit browser input and one fenced source retry."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from katcha.acquisition import worker as module
from katcha.acquisition.fetch import FetchFailure
from katcha.acquisition.human_browser import HumanBrowser
from katcha.acquisition.worker import Journal, Worker


def permission():
    return {
        "provider": "youtube",
        "operations": ["download"],
        "allowed_hosts": ["www.youtube.com", "youtube.com"],
        "evidence_scope": "synthetic fixture",
        "evidence_refs": ["fixture:allowed"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


class Browser:
    def __init__(self):
        self.commands, self.closed = [], []
        self.session = False

    def ready(self):
        return True

    def execute(self, channel, job, action, url, scope):
        self.commands.append(action)
        self.session = True
        return {"available": True, "snapshot_id": "fresh-view"}

    def export_cookies(self, channel, job, scope, destination):
        if not self.session:
            raise ValueError("Session was lost")
        destination.write_text("private-session")
        destination.chmod(0o600)
        return "fixture-browser-agent"

    def close(self, channel, job):
        self.closed.append(job)
        self.session = False


class Control:
    def __init__(self, channel, job):
        self.job = {
            "id": job,
            "status": "queued",
            "generation": 0,
            "heartbeat_sequence": 0,
            "bytes_received": 0,
            "max_bytes": 1024,
        }
        self.channel, self.requests = channel, []
        self.command = None
        self.state = "waiting"
        self.lose_fail = self.lose_resume = self.lose_result = False

    def lease(self):
        return {
            "job": dict(self.job),
            "fetch_url": "https://www.youtube.com/watch?v=fixture",
            "permission": permission(),
        }

    def __call__(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if method == "GET":
            return [] if "?" in path else dict(self.job)
        if path.endswith("/claim"):
            if self.job["status"] == "queued":
                self.job.update(status="leased", generation=1)
            return self.lease()
        if path.endswith("/heartbeat"):
            return dict(self.job)
        if path.endswith("/fail"):
            assert payload["generation"] == self.job["generation"]
            self.job["status"] = (
                "waiting_human" if payload.get("recovery_available") else "recovery_required"
            )
            if self.lose_fail:
                self.lose_fail = False
                raise httpx.ReadTimeout("lost failure acknowledgment")
            return dict(self.job)
        if path.endswith("/human/poll"):
            command, self.command = self.command, None
            return {
                "state": self.state,
                "command": command,
                "fetch_url": self.lease()["fetch_url"],
                "permission": permission(),
            }
        if path.endswith("/human/result"):
            if self.lose_result:
                self.lose_result = False
                raise httpx.ReadTimeout("lost action acknowledgment")
            return {"accepted": True}
        if path.endswith("/human/unavailable"):
            self.state = "waiting"
            return {"state": "waiting", "code": payload["code"]}
        if path.endswith("/human/resume"):
            if self.job["status"] == "waiting_human":
                self.job.update(status="leased", generation=self.job["generation"] + 1)
            assert payload["generation"] == self.job["generation"] - 1
            if self.lose_resume:
                self.lose_resume = False
                raise httpx.ReadTimeout("lost resume acknowledgment")
            return self.lease()
        if path.endswith("/upload"):
            return {"completed": True}
        if path.endswith("/upload/complete"):
            self.job["status"] = "awaiting_validation"
            return dict(self.job)
        raise AssertionError(path)


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    channel, job = str(uuid.uuid4()), str(uuid.uuid4())
    journal = Journal(tmp_path / "state", channel, job)
    browser = Browser()
    control = Control(channel, job)
    worker = Worker(
        "https://control.example",
        "worker-token",
        [channel],
        tmp_path / "state",
        human_browser=browser,
    )
    worker.request = control
    calls = []

    def extract(url, scope, directory, limit, progress, *, cookie_file=None, user_agent=None):
        calls.append((cookie_file, user_agent))
        if cookie_file is None:
            raise FetchFailure("authentication_required")
        assert cookie_file.read_text() == "private-session"
        assert cookie_file.stat().st_mode & 0o077 == 0
        assert user_agent == "fixture-browser-agent"
        media = directory / "media.mp4"
        media.write_bytes(b"fixture-media")
        return media

    monkeypatch.setattr(module, "fetch_extracted", extract)
    yield worker, journal, browser, control, calls
    worker.client.close()
    worker.storage_client.close()


def open_session(worker, journal, browser, control):
    control.command = {"id": str(uuid.uuid4()), "action": {"action": "open"}}
    worker.run_job(journal)
    assert browser.session


def test_auth_wait_browser_and_one_retry_preserve_job_and_require_validation(recovery):
    worker, journal, browser, control, calls = recovery
    claim = journal.state["claim_id"]
    worker.run_job(journal)
    assert journal.state["phase"] == "waiting_human"
    assert control.job["status"] == "waiting_human"
    open_session(worker, journal, browser, control)
    control.state = "retry_requested"
    worker.run_job(journal)
    assert control.job["status"] == "awaiting_validation"
    assert journal.state["claim_id"] == claim and journal.state["generation"] == 2
    assert len(calls) == 2
    assert not list(journal.directory.glob("*.cookies"))
    assert journal.state["job_id"] in browser.closed
    worker.run_job(journal)
    assert len(calls) == 2
    assert "private-session" not in json.dumps(control.requests)


def test_lost_fail_response_does_not_repeat_source_request(recovery):
    worker, journal, browser, control, calls = recovery
    control.lose_fail = True
    with pytest.raises(httpx.ReadTimeout):
        worker.run_job(journal)
    assert journal.state["phase"] == "reporting_failure"
    worker.run_job(Journal(worker.state_dir, journal.state["channel"], journal.state["job_id"]))
    assert len(calls) == 1
    assert control.job["status"] == "waiting_human"


def test_lost_resume_response_reuses_old_identity_and_downloads_once(recovery):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    open_session(worker, journal, browser, control)
    control.state, control.lose_resume = "retry_requested", True
    with pytest.raises(httpx.ReadTimeout):
        worker.run_job(journal)
    assert control.job["generation"] == 2 and journal.state["generation"] == 1
    assert journal.state["phase"] == "human_resuming"
    worker.run_job(Journal(worker.state_dir, journal.state["channel"], journal.state["job_id"]))
    resumes = [p for _, path, p in control.requests if path.endswith("/human/resume")]
    assert len(resumes) == 2 and resumes[0] == resumes[1]
    assert len(calls) == 2 and control.job["status"] == "awaiting_validation"


def test_restart_without_browser_requires_reopen_before_retry(recovery):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    control.state = "retry_requested"
    worker.run_job(Journal(worker.state_dir, journal.state["channel"], journal.state["job_id"]))
    assert control.state == "waiting" and len(calls) == 1
    assert not any(path.endswith("/human/resume") for _, path, _ in control.requests)


def test_interrupted_retry_is_quarantined_without_source_replay(recovery):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    journal.update(phase="acquiring", generation=2, transport="extractor", human_retry=True)
    control.job.update(status="leased", generation=2)
    (journal.directory / "human-session.cookies").write_text("leftover")
    worker.run_job(journal)
    assert len(calls) == 1 and control.job["status"] == "recovery_required"
    assert not (journal.directory / "human-session.cookies").exists()


def test_lost_action_result_is_not_replayed(recovery):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    control.command = {"id": str(uuid.uuid4()), "action": {"action": "type", "text": "secret"}}
    control.lose_result = True
    with pytest.raises(httpx.ReadTimeout):
        worker.run_job(journal)
    worker.run_job(journal)
    assert len(browser.commands) == 1 and len(calls) == 1
    assert "secret" not in journal.path.read_text()


def test_cancellation_closes_browser_and_removes_cookie(recovery):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    open_session(worker, journal, browser, control)
    (journal.directory / "human-session.cookies").write_text("leftover")
    control.job["status"] = "cancelled"
    worker.run_job(journal)
    assert journal.state["phase"] == "done" and browser.closed
    assert not list(journal.directory.glob("*.cookies")) and len(calls) == 1


def test_waiting_job_releases_media_lock_and_does_not_starve_queue(recovery, monkeypatch):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    worker.resource_lock_path = journal.directory / "resource.lock"
    monkeypatch.setattr(module, "resource_slot", lambda *a, **k: pytest.fail("waiting holds lock"))
    worker.once()
    assert any("?status=queued" in path for _, path, _ in control.requests)


def test_cookie_export_scopes_secrets_and_sets_private_mode(tmp_path):
    @asynccontextmanager
    async def lock():
        yield

    async def cookies():
        return [
            {
                "name": "session",
                "value": "allowed",
                "domain": ".youtube.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": -1,
            },
            {"name": "secret", "value": "excluded", "domain": ".google.com", "expires": -1},
            {"name": "bad", "value": "bad\nrecord", "domain": ".youtube.com", "expires": -1},
        ]

    async def evaluate(_):
        return "browser-agent"

    async def session(*args):
        return SimpleNamespace(
            created=time.monotonic(),
            lock=lock(),
            context=SimpleNamespace(cookies=cookies),
            page=SimpleNamespace(evaluate=evaluate),
        )

    browser = HumanBrowser.__new__(HumanBrowser)
    browser.pool = SimpleNamespace(session=session)
    browser._run = asyncio.run
    destination = tmp_path / "session.cookies"
    assert browser.export_cookies("channel", "job", permission(), destination) == "browser-agent"
    assert "allowed" in destination.read_text()
    assert "excluded" not in destination.read_text() and "bad" not in destination.read_text()
    assert os.stat(destination).st_mode & 0o077 == 0


def test_missing_saved_cookie_after_lost_resume_quarantines_without_replay(recovery):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    open_session(worker, journal, browser, control)
    control.state, control.lose_resume = "retry_requested", True
    with pytest.raises(httpx.ReadTimeout):
        worker.run_job(journal)
    (journal.directory / "human-session.cookies").unlink()
    worker.run_job(journal)
    assert len(calls) == 1 and control.job["status"] == "recovery_required"
    assert journal.state["phase"] == "done"


def test_runtime_unavailable_never_advertises_recovery(recovery):
    worker, journal, browser, control, calls = recovery
    browser.ready = lambda: False
    worker.run_job(journal)
    failures = [p for _, path, p in control.requests if path.endswith("/fail")]
    assert len(calls) == 1 and "recovery_available" not in failures[0]
    assert journal.state["phase"] == "done"


@pytest.mark.parametrize("denied", ["status", "poll"])
def test_revoked_control_closes_browser_without_discarding_job(recovery, denied):
    worker, journal, browser, control, calls = recovery
    worker.run_job(journal)
    open_session(worker, journal, browser, control)
    original = worker.request

    def revoked(method, path, payload=None):
        if (denied == "status" and method == "GET") or (
            denied == "poll" and path.endswith("/human/poll")
        ):
            raise module.LeaseLost("Control request refused (403)")
        return original(method, path, payload)

    worker.request = revoked
    with pytest.raises(module.LeaseLost):
        worker.run_job(journal)
    assert browser.closed and not browser.session
    assert journal.state["phase"] == "waiting_human"
    assert len(calls) == 1
