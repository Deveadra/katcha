import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("runtime", Path(__file__).parents[1] / "runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def instance(tmp_path):
    (tmp_path / ".env.example").write_text(
        "KATCHA_RENDER_BACKEND=local\nKATCHA_OPENAI_API_KEY=private-key\n"
    )
    return runtime.Runtime(tmp_path)


def test_bootstrap_preserves_keys_and_unrelated_settings(tmp_path):
    app = instance(tmp_path)
    key = app.values["KATCHA_CREDENTIAL_ENCRYPTION_KEY"]
    app.bootstrap()
    assert app.values["KATCHA_CREDENTIAL_ENCRYPTION_KEY"] == key
    assert app.values["KATCHA_OPENAI_API_KEY"] == "private-key"
    assert app.env_path.stat().st_mode & 0o777 == 0o600
    assert "private-key" not in json.dumps(app.snapshot())


def test_diagnostics_redact_and_preserve_valid_json(tmp_path):
    app = instance(tmp_path)
    app.event("error", "renderer", "private-key Bearer secret-value password=bad-value")
    row = json.loads((app.directory / "events.jsonl").read_text())
    assert "private-key" not in row["message"]
    assert "secret-value" not in row["message"]
    assert "bad-value" not in row["message"]
    assert row["session"] == app.session


def test_aws_overlay_and_shell_isolation(tmp_path):
    app = instance(tmp_path)
    with patch.dict(
        runtime.os.environ, {"KATCHA_RENDER_BACKEND": "lambda", "AWS_SECRET_ACCESS_KEY": "bad"}
    ):
        assert "AWS_SECRET_ACCESS_KEY" not in app.environment()
        assert not any("aws-render" in x for x in app.command())
    app.save({"KATCHA_RENDER_BACKEND": "lambda", "KATCHA_AWS_SIGNING_HELPER_PATH": "/helper"})
    assert any("aws-roles-anywhere" in x for x in app.command())


def test_failed_start_retains_actionable_diagnostic(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    with patch.object(app, "run", side_effect=FileNotFoundError("docker not found")):
        app._operate("start")
    assert app.phase == "failed"
    assert not app.lock.locked()
    assert any("recovery" in e for e in app.events)


def test_command_timeout_and_nonzero_exit(tmp_path):
    app = instance(tmp_path)
    import pytest

    with pytest.raises(TimeoutError):
        app.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
    with pytest.raises(RuntimeError):
        app.run([sys.executable, "-c", "raise SystemExit(2)"])


def test_http_rejects_foreign_origin_and_secret_exposure(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/runtime/start",
            "{}",
            {"Origin": "https://evil.example", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", "/runtime/status")
        response = connection.getresponse()
        assert response.status == 200
        assert b"private-key" not in response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_restart_redacts_existing_credentials(tmp_path):
    original = instance(tmp_path)
    restarted = runtime.Runtime(tmp_path)
    assert restarted.redact("private-key") == "[REDACTED]"
    assert (
        restarted.values["KATCHA_CONTROL_API_TOKEN"] == original.values["KATCHA_CONTROL_API_TOKEN"]
    )


def test_readiness_requires_core_services_but_not_all_workers(tmp_path):
    app = instance(tmp_path)
    from unittest.mock import MagicMock

    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    core = json.dumps(
        [
            {"Service": service, "State": "running", "Health": "healthy", "ExitCode": 0}
            for service in runtime.CORE_REQUIRED
        ]
    )
    with (
        patch.object(app, "run", return_value=core),
        patch.object(runtime.http.client, "HTTPConnection", return_value=connection),
    ):
        app.check()
    assert app.phase == "ready"
    assert app.background_phase != "ready"

    with (
        patch.object(app, "run", return_value='[{"Service":"api","State":"running"}]'),
        patch.object(runtime.http.client, "HTTPConnection", return_value=connection),
    ):
        app.check()
    assert app.phase == "degraded"


def test_stop_never_removes_volumes(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    with patch.object(app, "run", return_value="2.30.0") as run:
        app._operate("stop")
    assert app.phase == "stopped"
    commands = [call.args[0] for call in run.call_args_list]
    assert any("stop" in cmd for cmd in commands)
    assert not any("down" in cmd or "-v" in cmd for cmd in commands)


def test_single_operation_lock(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    try:
        assert app.operate("start") is False
    finally:
        app.lock.release()


def test_redaction_does_not_corrupt_diagnostic_schema(tmp_path):
    app = instance(tmp_path)
    app.save({"KATCHA_S3_ACCESS_KEY": "katcha"})
    app.event("error", "launcher", "value katcha")
    row = json.loads((app.directory / "events.jsonl").read_text())
    assert row["schema"] == "katcha.diagnostic.v1"
    assert row["message"] == "value [REDACTED]"


def test_start_reuses_images_and_opens_workspace_before_background(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if "version" in args:
            return "2.30.0"
        return ""

    with (
        patch.object(app, "run", side_effect=fake_run),
        patch.object(app, "start_logs"),
        patch.object(app, "check", side_effect=lambda: setattr(app, "phase", "ready")),
        patch.object(app, "_warm_background") as warm,
    ):
        app._operate("start")
        if app.background_thread:
            app.background_thread.join(timeout=1)

    up = next(cmd for cmd in calls if "up" in cmd)
    assert "--no-build" in up
    assert up[-1] == "api"
    assert not any("build" in cmd for cmd in calls)
    assert app.phase == "ready"
    assert app.stage == "ready"
    assert app.snapshot()["operation_elapsed_seconds"] is None
    assert not app.lock.locked()
    warm.assert_called_once()


def test_start_builds_only_core_when_workspace_images_are_missing(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    calls = []
    failed_once = False

    def fake_run(args, **_kwargs):
        nonlocal failed_once
        calls.append(args)
        if "version" in args:
            return "2.30.0"
        if "up" in args and "--no-build" in args and not failed_once:
            failed_once = True
            raise RuntimeError("pull access denied for katcha-api, repository does not exist")
        return ""

    with (
        patch.object(app, "run", side_effect=fake_run),
        patch.object(app, "start_logs"),
        patch.object(app, "check", side_effect=lambda: setattr(app, "phase", "ready")),
        patch.object(app, "_warm_background"),
    ):
        app._operate("start")

    build = next(cmd for cmd in calls if "build" in cmd)
    assert set(runtime.CORE_BUILD_SERVICES) <= set(build)
    assert all(service not in build for service in runtime.BACKGROUND_SERVICES)
    assert app.phase == "ready"


def test_reconcile_existing_runtime_without_relaunch(tmp_path):
    app = instance(tmp_path)
    from unittest.mock import MagicMock

    required = [
        "postgres",
        "temporal",
        "minio",
        "api",
        "worker",
        "analysis-worker",
        "renderer",
        "production-worker",
        "longform-worker",
        "publishing-worker",
        "discovery-worker",
        "trends-worker",
        "intelligence-worker",
    ]
    payload = json.dumps(
        [
            {"Service": service, "State": "running", "Health": "healthy", "ExitCode": 0}
            for service in required
        ]
    )
    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    with (
        patch.object(app, "run", return_value=payload) as run,
        patch.object(app, "start_logs"),
        patch.object(runtime.http.client, "HTTPConnection", return_value=connection),
    ):
        assert app.reconcile_existing() is True

    commands = [call.args[0] for call in run.call_args_list]
    assert app.phase == "ready"
    assert app.stage == "ready"
    assert all("build" not in command and "up" not in command for command in commands)
    assert any(
        event["message"] == "Reattached to the existing Katcha runtime."
        for event in app.events
    )


def test_reconcile_empty_runtime_stays_idle(tmp_path):
    app = instance(tmp_path)
    with patch.object(app, "run", return_value="[]"), patch.object(app, "start_logs") as logs:
        assert app.reconcile_existing() is True
    assert app.phase == "idle"
    assert app.stage == "idle"
    logs.assert_not_called()


def test_reconcile_stopped_runtime_stays_stopped(tmp_path):
    app = instance(tmp_path)
    payload = json.dumps(
        [
            {"Service": "api", "State": "exited", "Health": "", "ExitCode": 0},
            {"Service": "worker", "State": "exited", "Health": "", "ExitCode": 0},
        ]
    )
    with patch.object(app, "run", return_value=payload), patch.object(app, "start_logs") as logs:
        assert app.reconcile_existing() is True
    assert app.phase == "stopped"
    assert app.stage == "stopped"
    logs.assert_not_called()


def test_reconcile_runtime_failure_is_recoverable(tmp_path):
    app = instance(tmp_path)
    with patch.object(app, "run", side_effect=OSError("Docker unavailable")):
        assert app.reconcile_existing() is False
    assert app.phase == "degraded"
    assert app.stage == "waiting for runtime"
    assert any(event.get("recovery") for event in app.events)


def test_background_warmup_builds_only_when_images_are_missing(tmp_path):
    app = instance(tmp_path)
    app.phase = "ready"
    calls = []
    failed_once = False

    def fake_run(args, **_kwargs):
        nonlocal failed_once
        calls.append(args)
        if "up" in args and "--no-build" in args and not failed_once:
            failed_once = True
            raise RuntimeError("no such image: katcha-renderer")
        return ""

    with (
        patch.object(app, "run", side_effect=fake_run),
        patch.object(app, "check"),
    ):
        app._warm_background()

    build = next(cmd for cmd in calls if "build" in cmd)
    assert set(runtime.BACKGROUND_SERVICES) <= set(build)
    assert app.background_phase == "ready"
