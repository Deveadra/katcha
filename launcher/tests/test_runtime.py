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
    assert len(app.values["KATCHA_TELEGRAM_PAIRING_CODE"]) >= 8
    assert "KATCHA_TELEGRAM_REVIEW_STORAGE_ENDPOINT_URL" in app.values
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


def test_start_opens_workspace_before_warming_full_stack(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if "version" in args:
            return "2.30.0"
        return ""

    def workspace_ready():
        app.workspace_ready = True
        return True

    with (
        patch.object(app, "run", side_effect=fake_run),
        patch.object(app, "start_logs"),
        patch.object(app, "probe_workspace", side_effect=workspace_ready),
        patch.object(app, "check", return_value="ready"),
    ):
        app._operate("start")

    workspace_build = next(
        i for i, cmd in enumerate(calls)
        if "build" in cmd and cmd[-1:] == ["api"]
    )
    workspace_up = next(
        i for i, cmd in enumerate(calls)
        if "up" in cmd and cmd[-1:] == ["api"]
    )
    early_up = next(
        i
        for i, cmd in enumerate(calls)
        if "up" in cmd and all(service in cmd for service in runtime.EARLY_AUTOMATION_SERVICES)
    )
    full_build = next(
        i for i, cmd in enumerate(calls)
        if "build" in cmd and all(service in cmd for service in runtime.BACKGROUND_BUILD_SERVICES)
    )
    full_up = next(
        i
        for i, cmd in enumerate(calls)
        if "up" in cmd
        and cmd[-1:] != ["api"]
        and not all(service in cmd for service in runtime.EARLY_AUTOMATION_SERVICES)
    )
    assert workspace_build < workspace_up
    assert workspace_up < early_up < full_build < full_up
    assert any(
        event["message"] == "Workspace ready; warming automation in the background."
        for event in app.events
    )
    assert any(
        event["message"] == "Discovery intelligence is online; warming the media factory."
        for event in app.events
    )
    assert app.phase == "ready"
    assert app.stage == "ready"
    assert app.snapshot()["operation_elapsed_seconds"] is None
    assert not app.lock.locked()


def test_background_warmup_failure_preserves_usable_workspace(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if "version" in args:
            return "2.30.0"
        if (
            "build" in args
            and all(service in args for service in runtime.BACKGROUND_BUILD_SERVICES)
        ):
            raise RuntimeError("renderer build failed")
        return ""

    def workspace_ready():
        app.workspace_ready = True
        return True

    with (
        patch.object(app, "run", side_effect=fake_run),
        patch.object(app, "start_logs"),
        patch.object(app, "probe_workspace", side_effect=workspace_ready),
    ):
        app._operate("start")

    assert app.phase == "degraded"
    assert app.workspace_ready
    assert app.stage == "automation warm-up failed"
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
        "telegram-worker",
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
        assert any(
            "build" in call.args[0]
            and all(service in call.args[0] for service in runtime.BACKGROUND_BUILD_SERVICES)
            for call in run.call_args_list
        )
        run.reset_mock()
        app.prepare_images()
        assert not any(
            "build" in call.args[0]
            and all(service in call.args[0] for service in runtime.BACKGROUND_BUILD_SERVICES)
            for call in run.call_args_list
        )
        (source / "app.py").write_text("version = 2")
        app.prepare_images()
        assert any(
            "build" in call.args[0]
            and all(service in call.args[0] for service in runtime.BACKGROUND_BUILD_SERVICES)
            for call in run.call_args_list
        )


def test_deleted_images_rebuild(tmp_path):
    app = instance(tmp_path)
    (app.directory / "build-fingerprint").write_text(app.build_fingerprint())
    with patch.object(app, "run", side_effect=[RuntimeError("missing"), ""]) as run:
        app.prepare_images()
    assert "build" in run.call_args.args[0]
    assert all(service in run.call_args.args[0] for service in runtime.BACKGROUND_BUILD_SERVICES)


def test_failed_build_does_not_write_cache(tmp_path):
    import pytest
    app = instance(tmp_path)
    with (
        patch.object(app, "run", side_effect=RuntimeError("build failed")),
        pytest.raises(RuntimeError),
    ):
        app.prepare_images()
    assert not (app.directory / "build-fingerprint").exists()


def test_workspace_image_cache_is_independent_from_heavy_renderer_inputs(tmp_path):
    app = instance(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("version = 1")
    (tmp_path / "Dockerfile.control").write_text("FROM python:3.12-slim\n")
    renderer = tmp_path / "renderer"
    renderer.mkdir()
    (renderer / "index.mjs").write_text("version = 1")
    workspace_before = app.build_fingerprint(workspace=True)
    full_before = app.build_fingerprint()
    (renderer / "index.mjs").write_text("version = 2")
    assert app.build_fingerprint(workspace=True) == workspace_before
    assert app.build_fingerprint() != full_before


def test_workspace_image_cache_skips_unchanged_control_plane_build(tmp_path):
    app = instance(tmp_path)
    (tmp_path / "Dockerfile.control").write_text("FROM python:3.12-slim\n")
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("version = 1")
    with patch.object(app, "run", return_value="") as run:
        app.prepare_workspace_image()
        assert any(call.args[0][-2:] == ["build", "api"] for call in run.call_args_list)
        run.reset_mock()
        app.prepare_workspace_image()
        assert not any(call.args[0][-2:] == ["build", "api"] for call in run.call_args_list)


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



def test_workspace_probe_uses_control_plane_readiness(tmp_path):
    app = instance(tmp_path)
    from unittest.mock import MagicMock

    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    with patch.object(runtime.http.client, "HTTPConnection", return_value=connection):
        assert app.probe_workspace() is True
    connection.request.assert_called_once_with("GET", "/v1/health/workspace")


def test_launcher_serves_workspace_shell_without_api(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/editing")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/editing/assets/editing.html"
        response.read()
        connection.request("GET", "/editing/assets/editing.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"Editing control center" in body
        connection.request("GET", "/clips")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/editing/assets/clips.html"
        response.read()
        connection.request("GET", "/editing/assets/clips.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"Clip library" in body
        connection.request("GET", "/channels")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/channels/assets/channels.html"
        response.read()
        connection.request("GET", "/channels/assets/channels.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"Channel Studio" in body
        connection.request("GET", "/ai")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/ai/assets/ai.html"
        response.read()
        connection.request("GET", "/ai/assets/ai.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"Katcha AI" in body
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_ready_is_not_published_before_operation_unlock(tmp_path):
    app = instance(tmp_path)
    app.lock.acquire()
    observed = []

    def fake_run(args, **_kwargs):
        if "version" in args:
            return "2.30.0"
        return ""

    def full_check(publish=True):
        assert publish is False
        observed.append((app.phase, app.lock.locked()))
        return "ready"

    def record_ready(level, component, message, **details):
        if message == "Katcha automation is fully ready.":
            observed.append((app.phase, app.lock.locked()))

    def workspace_ready():
        app.workspace_ready = True
        return True

    with (
        patch.object(app, "run", side_effect=fake_run),
        patch.object(app, "start_logs"),
        patch.object(app, "probe_workspace", side_effect=workspace_ready),
        patch.object(app, "check", side_effect=full_check),
        patch.object(app, "event", side_effect=record_ready),
    ):
        app._operate("start")

    assert observed[0] == ("starting", True)
    assert observed[-1] == ("ready", False)
    assert app.phase == "ready"
    assert not app.lock.locked()


def test_prebuilt_workspace_image_avoids_local_build(tmp_path):
    app = instance(tmp_path)
    revision = "a" * 40
    with (
        patch.object(app, "release_revision", return_value=revision),
        patch.object(app, "run", return_value="") as run,
    ):
        app.prepare_workspace_image()

    commands = [call.args[0] for call in run.call_args_list]
    remote = f"{runtime.PREBUILT_REGISTRY}/katcha-control:{revision}"
    assert ["docker", "pull", remote] in commands
    assert ["docker", "tag", remote, runtime.WORKSPACE_IMAGE] in commands
    assert not any("build" in command for command in commands)
    assert any(event["message"] == "Using prebuilt workspace image." for event in app.events)


def test_prebuilt_failure_falls_back_to_local_build(tmp_path):
    app = instance(tmp_path)
    revision = "b" * 40

    def fake_run(args, **_kwargs):
        if args[:2] == ["docker", "pull"]:
            raise RuntimeError("registry unavailable")
        return ""

    with (
        patch.object(app, "release_revision", return_value=revision),
        patch.object(app, "run", side_effect=fake_run) as run,
    ):
        app.prepare_workspace_image()

    commands = [call.args[0] for call in run.call_args_list]
    assert any("build" in command and command[-1:] == ["api"] for command in commands)
    assert any(
        event["message"]
        == "Some prebuilt runtime images are unavailable; building only the missing images."
        for event in app.events
    )


def test_prebuilt_images_are_not_used_for_dirty_or_non_main_checkout(tmp_path):
    app = instance(tmp_path)
    with patch.object(app, "release_revision", return_value=None), patch.object(
        app, "run", return_value=""
    ) as run:
        app.prepare_workspace_image()
    commands = [call.args[0] for call in run.call_args_list]
    assert not any(command[:2] == ["docker", "pull"] for command in commands)
    assert any("build" in command and command[-1:] == ["api"] for command in commands)


def test_background_prebuilt_images_cover_every_local_runtime_image(tmp_path):
    app = instance(tmp_path)
    revision = "c" * 40
    images = app.built_images()
    with (
        patch.object(app, "release_revision", return_value=revision),
        patch.object(app, "run", return_value="") as run,
    ):
        app.prepare_images()

    commands = [call.args[0] for call in run.call_args_list]
    for image in images:
        remote = app.prebuilt_ref(image, revision)
        assert ["docker", "pull", remote] in commands
        assert ["docker", "tag", remote, image] in commands
    assert not any("build" in command for command in commands)


def test_health_monitor_lock_never_blocks_lifecycle_action(tmp_path):
    app = instance(tmp_path)
    app.health_lock.acquire()
    try:
        with patch.object(runtime.threading.Thread, "start"):
            assert app.operate("stop") is True
            assert app.lock.locked()
            app.lock.release()
    finally:
        app.health_lock.release()


def test_health_check_cannot_overwrite_active_lifecycle_phase(tmp_path):
    app = instance(tmp_path)
    app.phase = "stopping"
    with (
        patch.object(
            app,
            "run",
            return_value='[{"Service":"api","State":"running","Health":"healthy","ExitCode":0}]',
        ),
        patch.object(app, "probe_workspace", return_value=True),
    ):
        app.check()
    assert app.phase == "stopping"



def test_partial_prebuilt_images_build_only_missing_services(tmp_path):
    app = instance(tmp_path)
    revision = "d" * 40
    images = app.built_images()
    missing_image = "katcha-renderer:local"
    missing_remote = app.prebuilt_ref(missing_image, revision)

    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "manifest", "inspect"] and args[-1] == missing_remote:
            raise RuntimeError("not published")
        return ""

    with (
        patch.object(app, "release_revision", return_value=revision),
        patch.object(app, "run", side_effect=fake_run) as run,
    ):
        app.prepare_images()

    commands = [call.args[0] for call in run.call_args_list]
    build_commands = [command for command in commands if "build" in command]
    assert len(build_commands) == 1
    assert build_commands[0][-1:] == ["renderer"]
    for image in images:
        remote = app.prebuilt_ref(image, revision)
        if image == missing_image:
            assert ["docker", "pull", remote] not in commands
        else:
            assert ["docker", "pull", remote] in commands
            assert ["docker", "tag", remote, image] in commands


def test_prebuilt_probe_is_bounded_before_workspace_fallback(tmp_path):
    app = instance(tmp_path)
    revision = "e" * 40
    remote = app.prebuilt_ref(runtime.WORKSPACE_IMAGE, revision)

    def fake_run(args, **kwargs):
        if args == ["docker", "manifest", "inspect", remote]:
            assert kwargs["timeout"] == 10
            raise TimeoutError("registry probe timed out")
        return ""

    with (
        patch.object(app, "release_revision", return_value=revision),
        patch.object(app, "run", side_effect=fake_run) as run,
    ):
        app.prepare_workspace_image()

    commands = [call.args[0] for call in run.call_args_list]
    assert ["docker", "pull", remote] not in commands
    assert any("build" in command and command[-1:] == ["api"] for command in commands)
