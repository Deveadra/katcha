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


def test_readiness_requires_all_workers(tmp_path):
    app = instance(tmp_path)
    from unittest.mock import MagicMock

    connection = MagicMock()
    connection.getresponse.return_value.status = 200
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


def test_start_builds_before_starting_services_and_reports_stage(tmp_path):
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
        patch.object(app, "probe_workspace", return_value=True),
        patch.object(app, "check", side_effect=lambda: setattr(app, "phase", "ready")),
    ):
        app._operate("start")

    core_build = next(cmd for cmd in calls if cmd[-2:] == ["build", "api"])
    full_build = next(cmd for cmd in calls if cmd[-1:] == ["build"])
    up_commands = [cmd for cmd in calls if "up" in cmd]
    assert core_build
    assert full_build
    assert len(up_commands) == 2
    assert up_commands[0][-1] == "api"
    assert all("--build" not in cmd for cmd in up_commands)
    assert app.phase == "ready"
    assert app.stage == "ready"
    assert app.snapshot()["operation_elapsed_seconds"] is None
    assert not app.lock.locked()



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


def test_unchanged_images_skip_build_and_source_changes_invalidate(tmp_path):
    app = instance(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("version = 1")
    with patch.object(app, "run", return_value="katcha-api") as run:
        app.prepare_images()
        assert any(call.args[0][-1] == "build" for call in run.call_args_list)
        run.reset_mock()
        app.prepare_images()
        assert not any(call.args[0][-1] == "build" for call in run.call_args_list)
        (source / "app.py").write_text("version = 2")
        app.prepare_images()
        assert any(call.args[0][-1] == "build" for call in run.call_args_list)


def test_deleted_images_rebuild(tmp_path):
    app = instance(tmp_path)
    (app.directory / "build-fingerprint").write_text(app.build_fingerprint())
    with patch.object(app, "run", side_effect=["image", RuntimeError("missing"), ""]) as run:
        app.prepare_images()
    assert run.call_args.args[0][-1] == "build"


def test_failed_build_does_not_write_cache(tmp_path):
    import pytest
    app = instance(tmp_path)
    with (
        patch.object(app, "run", side_effect=["image", RuntimeError("build failed")]),
        pytest.raises(RuntimeError),
    ):
        app.prepare_images()
    assert not (app.directory / "build-fingerprint").exists()


def test_run_intent_survives_restart_and_stop_clears_it(tmp_path):
    app = instance(tmp_path)
    with patch.object(runtime.threading.Thread, "start"):
        assert app.operate("start")
        app.lock.release()
        assert runtime.Runtime(tmp_path).desired_running
        assert app.operate("stop")
        app.lock.release()
        assert not runtime.Runtime(tmp_path).desired_running


def test_recovery_backoff_and_explicit_stop(tmp_path):
    app = instance(tmp_path)
    app.phase = "failed"
    app.desired_running = True
    app.retry_at = runtime.time.monotonic() + 100
    with patch.object(app, "probe_workspace"), patch.object(app, "operate") as operate:
        app.monitor_once()
        operate.assert_not_called()
        app.retry_at = 0
        app.monitor_once()
        operate.assert_called_once_with("start")
        operate.reset_mock()
        app.desired_running = False
        app.monitor_once()
        operate.assert_not_called()


def test_workspace_probe_runs_while_startup_lock_is_held(tmp_path):
    app = instance(tmp_path)
    app.phase = "starting"
    app.lock.acquire()
    with patch.object(app, "probe_workspace") as probe:
        app.monitor_once()
        probe.assert_called_once()
    app.lock.release()


def test_recovery_recreates_missing_worker_even_when_remaining_services_run(tmp_path):
    app = instance(tmp_path)
    app.desired_running = True
    app.phase = "degraded"
    app.health_check_at = runtime.time.monotonic() + 100
    app.services = [
        {"Service": service, "State": "running"}
        for service in runtime.REQUIRED_SERVICES - {"discovery-worker"}
    ]
    with patch.object(app, "probe_workspace"), patch.object(app, "operate") as operate:
        app.monitor_once()
        operate.assert_called_once_with("start")


def test_unhealthy_running_containers_are_not_restart_looped(tmp_path):
    app = instance(tmp_path)
    app.desired_running = True
    app.phase = "degraded"
    app.health_check_at = runtime.time.monotonic() + 100
    app.services = [
        {"Service": service, "State": "running", "Health": "unhealthy"}
        for service in runtime.REQUIRED_SERVICES
    ]
    with patch.object(app, "probe_workspace"), patch.object(app, "operate") as operate:
        app.monitor_once()
        operate.assert_not_called()



def test_core_image_cache_is_independent_of_renderer(tmp_path):
    app = instance(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("version = 1")
    renderer = tmp_path / "renderer"
    renderer.mkdir()
    (renderer / "package.json").write_text('{"version":"1"}')

    with patch.object(app, "run", return_value="") as run:
        app.prepare_core_image()
        assert any(call.args[0][-2:] == ["build", "api"] for call in run.call_args_list)
        run.reset_mock()
        app.prepare_core_image()
        assert not any(call.args[0][-2:] == ["build", "api"] for call in run.call_args_list)
        (renderer / "package.json").write_text('{"version":"2"}')
        app.prepare_core_image()
        assert not any(call.args[0][-2:] == ["build", "api"] for call in run.call_args_list)
        (source / "app.py").write_text("version = 2")
        app.prepare_core_image()
        assert any(call.args[0][-2:] == ["build", "api"] for call in run.call_args_list)


def test_background_failure_preserves_workspace_access(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if "version" in args:
            return "2.30.0"
        if args[-1:] == ["build"] and args[-2:] != ["build", "api"]:
            raise RuntimeError("renderer build failed")
        return ""

    with (
        patch.object(app, "run", side_effect=fake_run),
        patch.object(app, "start_logs"),
        patch.object(app, "probe_workspace", return_value=True),
    ):
        app.workspace_ready = True
        app._operate("start")

    assert app.workspace_ready is True
    assert app.phase == "degraded"
    assert app.stage == "background capability warm-up failed"



def test_visible_services_color_code_and_missing_dependencies(tmp_path):
    app = instance(tmp_path)
    app.services = [
        {"Service": "api", "State": "running", "Health": "healthy", "ExitCode": 0},
        {"Service": "renderer", "State": "running", "Health": "starting", "ExitCode": 0},
        {"Service": "worker", "State": "exited", "Health": "", "ExitCode": 1},
        {"Service": "migrate", "State": "exited", "Health": "", "ExitCode": 0},
    ]
    rows = {row["Service"]: row for row in app.visible_services()}
    assert rows["api"]["StatusColor"] == "green"
    assert rows["renderer"]["StatusColor"] == "yellow"
    assert rows["worker"]["StatusColor"] == "red"
    assert rows["migrate"]["StatusColor"] == "green"
    assert rows["temporal"]["StatusColor"] == "red"
    assert rows["temporal"]["Missing"] is True


def test_service_repair_is_targeted_and_never_runs_global_start(tmp_path):
    app = instance(tmp_path)
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if "ps" in args:
            return "[]"
        return ""

    with patch.object(app, "run", side_effect=fake_run), patch.object(app, "check"):
        assert app.repair_service("renderer", "restart")
        thread = app.lock
        for _ in range(100):
            if not thread.locked():
                break
            runtime.time.sleep(0.01)

    commands = calls
    assert any("restart" in cmd and cmd[-1] == "renderer" for cmd in commands)
    assert not any("build" in cmd for cmd in commands)
    assert not any("up" in cmd for cmd in commands)


def test_service_rebuild_only_builds_selected_image(tmp_path):
    app = instance(tmp_path)
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return ""

    with patch.object(app, "run", side_effect=fake_run), patch.object(app, "check"):
        assert app.repair_service("renderer", "rebuild")
        for _ in range(100):
            if not app.lock.locked():
                break
            runtime.time.sleep(0.01)

    assert any(cmd[-2:] == ["build", "renderer"] for cmd in calls)
    assert any("force-recreate" in cmd and cmd[-1] == "renderer" for cmd in calls)
    assert not any(cmd[-1:] == ["build"] for cmd in calls)


def test_shared_core_rebuild_builds_api_image_for_worker_only(tmp_path):
    app = instance(tmp_path)
    calls = []

    with patch.object(app, "run", side_effect=lambda args, **_kwargs: calls.append(args) or ""), \
         patch.object(app, "check"):
        assert app.repair_service("discovery-worker", "rebuild")
        for _ in range(100):
            if not app.lock.locked():
                break
            runtime.time.sleep(0.01)

    assert any(cmd[-2:] == ["build", "api"] for cmd in calls)
    assert any(cmd[-1] == "discovery-worker" and "force-recreate" in cmd for cmd in calls)
