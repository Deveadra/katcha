"""The 0067 policy admits only the reviewed schema step and bootstrap import."""

import hashlib
import json
from pathlib import Path

import pytest
from test_production_deploy import NEW, OLD, _plan_fixture, module, responses


def recovery_fixture(app, *, env=True):
    fixture = responses()
    changes = [{"filename": app.RECOVERY_FILE, "status": "added"}]
    if env:
        changes.append({"filename": app.RECOVERY_ENV, "status": "modified"})
    fixture[f"/compare/{OLD}...{NEW}"]["files"] = changes
    for path, blob in {
        app.HANDOFF_FILE: app.HANDOFF_BLOB,
        app.RECOVERY_FILE: app.RECOVERY_BLOB,
        app.RECOVERY_ENV: app.RECOVERY_ENV_BLOB,
    }.items():
        if path != app.RECOVERY_ENV:
            fixture[f"/contents/migrations/versions?ref={NEW}"].append(
                {"name": Path(path).name, "type": "file", "sha": blob}
            )
        fixture[f"/contents/{path}?ref={NEW}"] = {"path": path, "type": "file", "sha": blob}
    fixture[f"/contents/{app.RECOVERY_ENV}?ref={OLD}"] = {
        "path": app.RECOVERY_ENV,
        "type": "file",
        "sha": app.RECOVERY_ENV_BEFORE,
    }
    return fixture


def provider(monkeypatch, app, fixture):
    monkeypatch.setattr(app, "github", lambda path: fixture[path])
    monkeypatch.setattr(
        app,
        "run",
        lambda *args, **kwargs: json.dumps(
            {
                "status": "ELIGIBLE_FOR_PREDEPLOY_REVIEW",
                "sha": NEW,
                "ci_run_id": 100,
                "image_run_id": 101,
            }
        ),
    )


@pytest.mark.parametrize("env", [False, True])
def test_reviewed_recovery_step_requires_explicit_policy_and_exact_blobs(monkeypatch, env):
    app = module()
    fixture = recovery_fixture(app, env=env)
    provider(monkeypatch, app, fixture)
    for policy in ("refuse", "approve-additive-through-0066"):
        with pytest.raises(app.Blocked):
            app.eligible(OLD, NEW, policy)
    assert app.eligible(OLD, NEW, "approve-additive-through-0067")
    for path, blob in (
        (app.RECOVERY_FILE, app.RECOVERY_BLOB),
        (app.RECOVERY_ENV, app.RECOVERY_ENV_BLOB),
    ):
        source = (Path(__file__).parents[1] / path).read_bytes()
        assert hashlib.sha1(f"blob {len(source)}".encode() + b"\0" + source).hexdigest() == blob
        fixture[f"/contents/{path}?ref={NEW}"]["sha"] = "f" * 40
        with pytest.raises(app.Blocked):
            app.eligible(OLD, NEW, "approve-additive-through-0067")
        fixture[f"/contents/{path}?ref={NEW}"]["sha"] = blob


@pytest.mark.parametrize(
    "change",
    [
        {
            "filename": "migrations/versions/0067_acquisition_human_recovery.py",
            "status": "modified",
        },
        {"filename": "migrations/env.py", "status": "modified"},
        {"filename": "migrations/versions/0068_unknown.py", "status": "added"},
    ],
)
def test_recovery_policy_does_not_admit_other_migration_changes(monkeypatch, change):
    app = module()
    fixture = recovery_fixture(app)
    fixture[f"/compare/{OLD}...{NEW}"]["files"] = [change]
    provider(monkeypatch, app, fixture)
    with pytest.raises(app.Blocked):
        app.eligible(OLD, NEW, "approve-additive-through-0067")


def test_bootstrap_base_is_pinned_and_revision_inventory_stays_bounded(monkeypatch):
    app = module()
    fixture = recovery_fixture(app)
    provider(monkeypatch, app, fixture)
    fixture[f"/contents/{app.RECOVERY_ENV}?ref={OLD}"]["sha"] = "f" * 40
    with pytest.raises(app.Blocked, match="bootstrap base"):
        app.eligible(OLD, NEW, "approve-additive-through-0067")
    fixture[f"/contents/{app.RECOVERY_ENV}?ref={OLD}"]["sha"] = app.RECOVERY_ENV_BEFORE
    fixture[f"/contents/migrations/versions?ref={NEW}"].append(
        {"name": "0068_future.py", "type": "file", "sha": "f" * 40}
    )
    with pytest.raises(app.Blocked, match="reviewed boundary"):
        app.eligible(OLD, NEW, "approve-additive-through-0067")


def test_recovery_preflight_requires_verified_handoff_and_absent_new_tables(monkeypatch):
    app = module()
    observations = {
        "revision": app.HANDOFF_REVISION,
        "challenge_table": "",
        "browser_command_table": "",
    }
    checked = []
    monkeypatch.setattr(app, "database_query", lambda name: observations[name])
    monkeypatch.setattr(app, "verify_handoff_schema", lambda: checked.append("handoff"))
    app.database_release_preflight(True, "approve-additive-through-0067")
    assert checked == ["handoff"]
    observations["challenge_table"] = "acquisition_challenges"
    with pytest.raises(app.Blocked, match="already exist"):
        app.database_release_preflight(True, "approve-additive-through-0067")
    observations["revision"] = app.LATEST_REVISION
    with pytest.raises(app.Blocked, match="verified 0066"):
        app.database_release_preflight(True, "approve-additive-through-0067")


def test_recovery_postflight_and_migration_free_releases_check_new_schema(monkeypatch):
    app = module()
    checked = []
    monkeypatch.setattr(app, "database_query", lambda name: app.RECOVERY_REVISION)
    monkeypatch.setattr(app, "verify_recovery_schema", lambda: checked.append("recovery"))
    app.database_release_postflight("approve-additive-through-0067")
    app.database_release_preflight(False, "refuse")
    app.database_release_postflight("refuse")
    assert checked == ["recovery"] * 3
    monkeypatch.setattr(app, "database_query", lambda name: app.HANDOFF_REVISION)
    with pytest.raises(app.Blocked, match="not upgraded"):
        app.database_release_postflight("approve-additive-through-0067")


def test_release_plan_recognizes_exact_recovery_step(monkeypatch):
    app, comparison, calls = _plan_fixture(monkeypatch, revision="0066_acquisition_handoff")
    comparison["files"] = [
        {"filename": app.RECOVERY_FILE, "status": "added"},
        {"filename": app.RECOVERY_ENV, "status": "modified"},
    ]
    result = app.plan_release(NEW)
    assert result["candidate_migration_policy"] == "approve-additive-through-0067"
    assert result["deployment_authorized"] is False
    assert ("schema", True, "approve-additive-through-0067") in calls
