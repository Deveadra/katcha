"""Exact recovery-release admission; no production operations or providers."""

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from test_production_deploy import (
    NEW,
    OLD,
    ROOT,
    _plan_fixture,
    module,
    poller,
    responses,
    signed_request,
)


def recovery_evidence(monkeypatch, *, changed=True):
    app = module()
    evidence = responses()
    files = [
        {"filename": app.RECOVERY_ENV_FILE, "status": "modified"},
        {"filename": app.RECOVERY_FILE, "status": "added"},
    ] if changed else [{"filename": "src/katcha/web/clips.js", "status": "modified"}]
    evidence[f"/compare/{OLD}...{NEW}"]["files"] = files
    for path, blob in {
        app.HANDOFF_FILE: app.HANDOFF_BLOB, app.RECOVERY_FILE: app.RECOVERY_BLOB,
        app.RECOVERY_ENV_FILE: app.RECOVERY_ENV_BLOB,
    }.items():
        evidence[f"/contents/{path}?ref={NEW}"] = {"path": path, "type": "file", "sha": blob}
        if path != app.RECOVERY_ENV_FILE:
            evidence[f"/contents/migrations/versions?ref={NEW}"].append({
                "name": Path(path).name, "type": "file", "sha": blob,
            })
    monkeypatch.setattr(app, "github", lambda path: evidence[path])
    monkeypatch.setattr(app, "run", lambda *a, **kw: json.dumps({
        "status": "ELIGIBLE_FOR_PREDEPLOY_REVIEW", "sha": NEW,
        "ci_run_id": 100, "image_run_id": 101,
    }))
    return app, evidence


def test_recovery_requires_explicit_policy_and_exact_artifacts(monkeypatch):
    app, _ = recovery_evidence(monkeypatch)
    for policy in app.MIGRATION_POLICIES - {app.RECOVERY_POLICY}:
        with pytest.raises(app.Blocked):
            app.eligible(OLD, NEW, policy)
    assert app.eligible(OLD, NEW, app.RECOVERY_POLICY)
    for path, expected in [(app.RECOVERY_FILE, app.RECOVERY_BLOB),
                           (app.RECOVERY_ENV_FILE, app.RECOVERY_ENV_BLOB)]:
        data = (ROOT / path).read_bytes()
        assert hashlib.sha1(f"blob {len(data)}".encode() + b"\0" + data).hexdigest() == expected


@pytest.mark.parametrize("drift", ["migration_blob", "environment_blob", "missing_environment",
                                  "modified_migration", "added_environment", "future_head"])
def test_recovery_rejects_modified_or_incomplete_artifacts(monkeypatch, drift):
    app, evidence = recovery_evidence(monkeypatch)
    files = evidence[f"/compare/{OLD}...{NEW}"]["files"]
    if drift in {"migration_blob", "environment_blob"}:
        path = app.RECOVERY_FILE if drift == "migration_blob" else app.RECOVERY_ENV_FILE
        evidence[f"/contents/{path}?ref={NEW}"]["sha"] = "f" * 40
    elif drift == "missing_environment":
        files.pop(0)
    elif drift == "modified_migration":
        files[1]["status"] = "modified"
    elif drift == "added_environment":
        files[0]["status"] = "added"
    else:
        evidence[f"/contents/migrations/versions?ref={NEW}"].append({
            "name": "0068_unreviewed.py", "type": "file", "sha": "f" * 40,
        })
    with pytest.raises(app.Blocked):
        app.eligible(OLD, NEW, app.RECOVERY_POLICY)


def test_later_recovery_release_still_pins_environment_and_migrations(monkeypatch):
    app, evidence = recovery_evidence(monkeypatch, changed=False)
    assert not app.eligible(OLD, NEW, "refuse")
    with pytest.raises(app.Blocked):
        app.eligible(OLD, NEW, app.RECOVERY_POLICY)
    evidence[f"/contents/{app.RECOVERY_ENV_FILE}?ref={NEW}"]["sha"] = "f" * 40
    with pytest.raises(app.Blocked):
        app.eligible(OLD, NEW, "refuse")


@pytest.mark.parametrize("revision", ["0065_acquisition_jobs", "0066_acquisition_handoff"])
def test_recovery_plan_only_upgrades_verified_0066(monkeypatch, revision):
    native = module()
    app, _, calls = _plan_fixture(monkeypatch, revision=revision, changes=[
        {"filename": native.RECOVERY_FILE, "status": "added"},
        {"filename": native.RECOVERY_ENV_FILE, "status": "modified"},
    ])
    if revision != app.HANDOFF_REVISION:
        with pytest.raises(app.Blocked, match="verified 0066"):
            app.plan_release(NEW)
        return
    result = app.plan_release(NEW)
    assert result["candidate_migration_policy"] == app.RECOVERY_POLICY
    assert result["schema_change"] is True
    assert result["deployment_authorized"] is False
    assert ("schema", True, app.RECOVERY_POLICY) in calls


def test_recovery_preflight_refuses_premature_tables_and_wrong_base(monkeypatch):
    app = module()
    observed = {"revision": app.HANDOFF_REVISION, "challenge_table": "",
                "browser_command_table": ""}
    monkeypatch.setattr(app, "database_query", lambda key: observed[key])
    checks = []
    monkeypatch.setattr(app, "verify_handoff_schema", lambda: checks.append("0066"))
    app.database_release_preflight(True, app.RECOVERY_POLICY)
    assert checks == ["0066"]
    observed["challenge_table"] = "acquisition_challenges"
    with pytest.raises(app.Blocked, match="already exist"):
        app.database_release_preflight(True, app.RECOVERY_POLICY)
    observed["revision"] = app.LATEST_REVISION
    with pytest.raises(app.Blocked, match="verified 0066 base"):
        app.database_release_preflight(True, app.RECOVERY_POLICY)


def test_recovery_policy_remains_bound_to_signed_request():
    app = poller()
    request, key = signed_request()
    payload = request["payload"]
    payload["migration_policy"] = "approve-additive-through-0067"
    payload["signature"] = hmac.new(key, app.canonical(payload), hashlib.sha256).hexdigest()
    assert app.authorized_request(request, key, 1500)["migration_policy"].endswith("0067")
    payload["migration_policy"] = "refuse"
    with pytest.raises(app.RequestBlocked, match="signature"):
        app.authorized_request(request, key, 1500)
