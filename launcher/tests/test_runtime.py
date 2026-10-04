import http.client
import importlib.util
import json
import socket
import struct
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SPEC = importlib.util.spec_from_file_location("runtime", Path(__file__).parents[1] / "runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_browser_socket_failures_are_identified_at_response_boundary(error, stage):
    handler = runtime.Handler.__new__(runtime.Handler)
    handler.wfile = MagicMock()
    if stage == "headers":
        with (
            patch.object(runtime.BaseHTTPRequestHandler, "end_headers", side_effect=error()),
            pytest.raises(runtime.ClientDisconnected),
        ):
            handler.end_headers()
    else:
        handler.wfile.write.side_effect = error()
        with pytest.raises(runtime.ClientDisconnected):
            handler.write_response(b"response")


def test_closed_browser_request_does_not_traceback_or_stop_status_server(tmp_path):
    app = instance(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class ObservedHandler(runtime.Handler):
        def handle(self):
            try:
                super().handle()
            finally:
                finished.set()

    def delayed_snapshot():
        entered.set()
        assert release.wait(3)
        return {"workspace_ready": True}

    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), ObservedHandler)
    server.runtime = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(server, "handle_error") as errors:
            with patch.object(app, "snapshot", side_effect=delayed_snapshot):
                client = socket.create_connection(("127.0.0.1", server.server_port), timeout=3)
                client.sendall(
                    (
                        "GET /runtime/status HTTP/1.0\r\n"
                        f"Host: 127.0.0.1:{server.server_port}\r\n\r\n"
                    ).encode()
                )
                assert entered.wait(3)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                client.close()
                release.set()
                assert finished.wait(3)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/runtime/status")
            response = connection.getresponse()
            assert response.status == 200
            assert "workspace_ready" in json.loads(response.read())
            connection.close()
            errors.assert_not_called()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_proxy_browser_disconnect_closes_upstream_without_gateway_failure():
    handler = runtime.Handler.__new__(runtime.Handler)
    handler.path = "/v1/fixture"
    handler.command = "GET"
    handler.headers = {}
    handler.rfile = MagicMock()
    handler.wfile = MagicMock()
    handler.wfile.write.side_effect = BrokenPipeError()
    handler.server = MagicMock()
    handler.server.runtime.values = {"KATCHA_CONTROL_API_TOKEN": "fixture"}
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = 200
    response.getheaders.return_value = []
    response.read.side_effect = [b"media", b""]
    with (
        patch.object(runtime.http.client, "HTTPConnection", return_value=connection),
        pytest.raises(runtime.ClientDisconnected),
    ):
        handler.proxy()
    connection.close.assert_called_once()
    handler.server.runtime.event.assert_not_called()


def test_handler_preserves_unexpected_application_errors():
    handler = runtime.Handler.__new__(runtime.Handler)
    with (
        patch.object(runtime.BaseHTTPRequestHandler, "handle", side_effect=ValueError("bug")),
        pytest.raises(ValueError, match="bug"),
    ):
        handler.handle()


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
    assert app.values["KATCHA_CHATGPT_HOST_ID"].startswith("urn:uuid:")
    host_id = app.values["KATCHA_CHATGPT_HOST_ID"]
    app.bootstrap()
    assert app.values["KATCHA_CHATGPT_HOST_ID"] == host_id
    assert app.values["KATCHA_OPENAI_API_KEY"] == "private-key"
    assert len(app.values["KATCHA_TELEGRAM_PAIRING_CODE"]) >= 8
    assert "KATCHA_TELEGRAM_REVIEW_STORAGE_ENDPOINT_URL" in runtime.FIELDS
    app.save({"KATCHA_TELEGRAM_BOT_TOKEN": "123456:fixture-bot-token"})
    assert app.values["KATCHA_TELEGRAM_BOT_TOKEN"] == "123456:fixture-bot-token"
    assert app.env_path.stat().st_mode & 0o777 == 0o600
    for name in ("incoming", "processed", "failed", "receipts"):
        directory = tmp_path / "handoff" / name
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o770
    assert app.environment()["KATCHA_HOST_GID"] == str(runtime.os.getgid())
    assert "private-key" not in json.dumps(app.snapshot())


def test_diagnostics_redact_and_preserve_valid_json(tmp_path):
    app = instance(tmp_path)
    app.event("error", "renderer", "private-key Bearer secret-value password=bad-value")
    row = json.loads((app.directory / "events.jsonl").read_text())
    assert "private-key" not in row["message"]
    assert "secret-value" not in row["message"]
    assert "bad-value" not in row["message"]
    assert row["session"] == app.session



def test_service_log_context_uses_source_severity_instead_of_error_word():
    line = (
        'temporal-1 | 2026-10-04T14:54:27.400036101Z '
        '{"level":"info","msg":"matching client encountered error",'
        '"service":"frontend","error":"Not enough hosts to serve the request",'
        '"service-error-type":"serviceerror.Unavailable"}'
    )
    level, details = runtime.service_log_context(line)
    assert level == "info"
    assert details["source_service"] == "temporal"
    assert details["source_time"] == "2026-10-04T14:54:27.400036101Z"
    assert details["source_message"] == "matching client encountered error"
    assert details["source_error"] == "Not enough hosts to serve the request"
    assert details["source_error_type"] == "serviceerror.Unavailable"


def test_service_log_context_marks_sigkill_exit_as_error_without_guessing_oom():
    level, details = runtime.service_log_context(
        "intelligence-worker-1 exited with code 137"
    )
    assert level == "error"
    assert details["source_service"] == "intelligence-worker"
    assert details["exit_code"] == 137
    assert details["exit_signal"] == 9
    assert "oom" not in json.dumps(details).lower()


def test_resource_snapshot_retains_oom_exit_and_restart_state(tmp_path):
    app = instance(tmp_path)
    inspected = [
        {
            "Config": {
                "Labels": {"com.docker.compose.service": "intelligence-worker"}
            },
            "State": {
                "Status": "exited",
                "OOMKilled": True,
                "ExitCode": 137,
                "Health": {"Status": "unhealthy"},
            },
            "RestartCount": 2,
        }
    ]
    with patch.object(
        app,
        "_diagnostic_command",
        side_effect=["abc", json.dumps(inspected), '{"CPUPerc":"99%"}'],
    ):
        result = app.resource_snapshot()

    assert result["docker_available"] is True
    assert result["container_state"] == [
        {
            "service": "intelligence-worker",
            "status": "exited",
            "oom_killed": True,
            "exit_code": 137,
            "restart_count": 2,
            "health": "unhealthy",
        }
    ]
    assert result["containers"][0]["CPUPerc"] == "99%"


def test_diagnostics_export_prepends_snapshot_and_uses_contextual_filename(tmp_path):
    app = instance(tmp_path)
    app.phase = "degraded"
    app.stage = "waiting for runtime"
    app.event("error", "fixture", "retained event")

    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        snapshot = {
            "schema": "katcha.diagnostic.snapshot.v1",
            "time": "2026-10-04T15:00:00+00:00",
            "session": app.session,
        }
        with patch.object(app, "diagnostic_snapshot", return_value=snapshot):
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=3
            )
            connection.request("GET", "/runtime/diagnostics")
            response = connection.getresponse()
            body = response.read().decode()
            disposition = response.getheader("Content-Disposition")
            connection.close()

        assert response.status == 200
        assert response.getheader("Content-Type") == "application/x-ndjson"
        assert disposition.startswith('attachment; filename="katcha-diagnostics-')
        assert f"-degraded-{app.session[:8]}.jsonl" in disposition
        rows = [json.loads(line) for line in body.splitlines() if line.strip()]
        assert rows[0]["schema"] == "katcha.diagnostic.snapshot.v1"
        assert rows[0]["journal_files"][0]["name"] == "events.jsonl"
        assert any(row.get("message") == "retained event" for row in rows[1:])
    finally:
        server.shutdown()
        server.server_close()


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
    assert not any(
        thread.name == "katcha-command-reader" and thread.is_alive()
        for thread in threading.enumerate()
    )

    with pytest.raises(RuntimeError):
        app.run([sys.executable, "-c", "raise SystemExit(2)"])
    assert not any(
        thread.name == "katcha-command-reader" and thread.is_alive()
        for thread in threading.enumerate()
    )


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


def test_handoff_upload_uses_launcher_local_transport(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    result = {
        "filename": "batch.json",
        "status": "processed",
        "size_bytes": 17,
        "modified_at": "2026-10-01T19:00:00Z",
        "channel_profile_id": "channel-one",
        "batch_key": "batch-001",
        "record_count": 2,
        "error": None,
        "receipt": {"created_count": 2, "updated_count": 0, "replayed": False},
    }
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch.object(
            runtime.Handler,
            "_api_json_request",
            return_value=(200, [result]),
        ) as api:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_port,
            )
            payload = b'{"handoff":"ok"}'
            connection.request(
                "POST",
                "/runtime/handoff/upload?filename=batch.json",
                payload,
                {"Content-Type": "application/octet-stream"},
            )
            response = connection.getresponse()
            assert response.status == 200
            body = json.loads(response.read())
            connection.close()

        assert body["status"] == "processed"
        assert body["record_count"] == 2
        assert (tmp_path / "handoff" / "incoming" / "batch.json").read_bytes() == payload
        api.assert_called_once_with(
            "POST",
            "/v1/intelligence-ingest/inbox/process",
            {"filenames": ["batch.json"], "limit": 1},
        )
        assert any(
            event["component"] == "handoff"
            and event["message"] == "Handoff upload received."
            for event in app.events
        )
    finally:
        server.shutdown()
        server.server_close()


def test_handoff_upload_rejects_conflicting_filename_content(tmp_path):
    app = instance(tmp_path)
    existing = tmp_path / "handoff" / "incoming" / "batch.json"
    existing.write_bytes(b"first")
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch.object(runtime.Handler, "_api_json_request") as api:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_port,
            )
            connection.request(
                "POST",
                "/runtime/handoff/upload?filename=batch.json",
                b"second",
                {"Content-Type": "application/octet-stream"},
            )
            response = connection.getresponse()
            assert response.status == 409
            assert "different content" in json.loads(response.read())["error"]
            connection.close()
        api.assert_not_called()
        assert existing.read_bytes() == b"first"
    finally:
        server.shutdown()
        server.server_close()


def test_handoff_upload_allows_filename_reuse_after_archive(tmp_path):
    app = instance(tmp_path)
    archived = tmp_path / "handoff" / "processed" / "batch.json"
    archived.write_bytes(b"old")
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result = [{
        "filename": "batch.json",
        "status": "processed",
        "record_count": 2,
    }]
    try:
        with patch.object(
            runtime.Handler,
            "_api_json_request",
            return_value=(200, result),
        ) as api:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_port,
            )
            connection.request(
                "POST",
                "/runtime/handoff/upload?filename=batch.json",
                b"new",
                {"Content-Type": "application/octet-stream"},
            )
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            connection.close()

        assert archived.read_bytes() == b"old"
        assert (tmp_path / "handoff" / "incoming" / "batch.json").read_bytes() == b"new"
        api.assert_called_once()
    finally:
        server.shutdown()
        server.server_close()


def test_handoff_upload_rejects_path_traversal(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/runtime/handoff/upload?filename=..%2Foutside.json",
            b"{}",
            {"Content-Type": "application/octet-stream"},
        )
        response = connection.getresponse()
        assert response.status == 400
        assert "filename" in json.loads(response.read())["error"].lower()
        connection.close()
        assert not (tmp_path / "outside.json").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_handoff_process_pending_uses_control_plane_api(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result = [{
        "filename": "pending.json",
        "status": "processed",
        "record_count": 3,
    }]
    try:
        with patch.object(
            runtime.Handler,
            "_api_json_request",
            return_value=(200, result),
        ) as api:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_port,
            )
            connection.request(
                "POST",
                "/runtime/handoff/process",
                json.dumps({"limit": 50}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == result
            connection.close()

        api.assert_called_once_with(
            "POST",
            "/v1/intelligence-ingest/inbox/process",
            {"limit": 50},
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("error", [RuntimeError, ConnectionResetError])
def test_proxy_exception_returns_controlled_gateway_error(tmp_path, error):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        original = http.client.HTTPConnection
        connection = original("127.0.0.1", server.server_port)
        with patch.object(
            runtime.http.client,
            "HTTPConnection",
            side_effect=error("fixture proxy failure"),
        ):
            connection.request("POST", "/v1/fixture", b"{}", {"Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 502
            body = json.loads(response.read())
        connection.close()
        assert "gateway" in body["error"].lower()
        assert any(
            event["component"] == "gateway"
            and error.__name__ in event["message"]
            for event in app.events
        )
    finally:
        server.shutdown()
        server.server_close()


def test_launcher_live_choice_enables_ai(tmp_path):
    app = instance(tmp_path)
    app.save({"KATCHA_AI_ENABLED": "false", "KATCHA_AI_EXECUTION_MODE": "fixture"})
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST", "/runtime/settings",
            json.dumps({"KATCHA_AI_EXECUTION_MODE": "live"}),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert app.values["KATCHA_AI_EXECUTION_MODE"] == "live"
        assert app.values["KATCHA_AI_ENABLED"] == "true"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_wsl_browser_open_uses_host_fallbacks(tmp_path):
    app = instance(tmp_path)
    failed = MagicMock(returncode=1, stderr="failed")
    succeeded = MagicMock(returncode=0, stderr="")
    with (
        patch.object(runtime, "_is_wsl", return_value=True),
        patch.object(runtime.subprocess, "run", side_effect=[failed, succeeded]) as run,
        patch.object(runtime.webbrowser, "open") as browser,
    ):
        assert runtime._open_browser("http://127.0.0.1:8765", app) is True

    assert run.call_count == 2
    assert run.call_args_list[0].args[0][0] == "powershell.exe"
    assert run.call_args_list[1].args[0][0] == "cmd.exe"
    browser.assert_not_called()


def test_browser_open_reports_manual_recovery_when_all_methods_fail(tmp_path, capsys):
    app = instance(tmp_path)
    failed = MagicMock(returncode=1, stderr="failed")
    with (
        patch.object(runtime, "_is_wsl", return_value=True),
        patch.object(runtime.subprocess, "run", return_value=failed),
        patch.object(runtime.webbrowser, "open") as browser,
    ):
        assert runtime._open_browser("http://127.0.0.1:8765", app) is False

    browser.assert_not_called()
    assert "Open http://127.0.0.1:8765" in capsys.readouterr().out
    assert any(
        event["component"] == "browser" and event.get("recovery")
        for event in app.events
    )



def test_linux_browser_open_uses_xdg_open(tmp_path):
    app = instance(tmp_path)
    with (
        patch.object(runtime, "_is_wsl", return_value=False),
        patch.object(runtime.sys, "platform", "linux"),
        patch.object(runtime.shutil, "which", return_value="/usr/bin/xdg-open"),
        patch.object(runtime.subprocess, "run", return_value=MagicMock(returncode=0)) as run,
        patch.object(runtime.webbrowser, "open") as browser,
    ):
        assert runtime._open_browser("http://127.0.0.1:8765", app) is True

    assert run.call_args.args[0] == ["/usr/bin/xdg-open", "http://127.0.0.1:8765"]
    assert "Opened Katcha in the desktop browser." in [e["message"] for e in app.events]
    browser.assert_not_called()


def test_linux_gio_failure_is_suppressed_and_manual_url_is_actionable(tmp_path, capsys):
    app = instance(tmp_path)
    failure = MagicMock(returncode=1, stderr="gio: Operation not supported")
    with (
        patch.object(runtime, "_is_wsl", return_value=False),
        patch.object(runtime.sys, "platform", "linux"),
        patch.object(runtime.shutil, "which", return_value="/usr/bin/xdg-open"),
        patch.object(runtime.subprocess, "run", return_value=failure),
        patch.object(runtime.webbrowser, "open") as browser,
    ):
        assert runtime._open_browser("http://127.0.0.1:8765", app) is False

    browser.assert_not_called()
    output = capsys.readouterr().out
    assert "gio:" not in output
    assert "Open http://127.0.0.1:8765" in output
    event = next(e for e in app.events if e["component"] == "browser")
    assert "Operation not supported" in event["attempts"][0]


def test_launcher_page_remains_available_when_browser_open_fails(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address, port = server.server_address
        connection = http.client.HTTPConnection(address, port, timeout=2)
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read()
        assert response.status == 200
        assert b"Start Katcha" in page
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

def test_stop_logs_reaps_launcher_follower_without_stopping_services(tmp_path):
    app = instance(tmp_path)
    follower = MagicMock()
    follower.poll.return_value = None
    follower.stdout = MagicMock()
    reader = MagicMock()
    reader.is_alive.return_value = False
    app.follow = follower
    app.follow_thread = reader

    app.stop_logs()

    follower.terminate.assert_called_once_with()
    follower.wait.assert_called_once_with(timeout=3)
    follower.kill.assert_not_called()
    reader.join.assert_called_once_with(timeout=3)
    follower.stdout.close.assert_called_once_with()
    assert app.follow is None
    assert app.follow_thread is None


def test_stop_logs_kills_stuck_follower(tmp_path):
    app = instance(tmp_path)
    follower = MagicMock()
    follower.poll.return_value = None
    follower.wait.side_effect = [runtime.subprocess.TimeoutExpired("logs", 3), None]
    follower.stdout = MagicMock()
    reader = MagicMock()
    reader.is_alive.return_value = False
    app.follow = follower
    app.follow_thread = reader

    app.stop_logs()

    follower.terminate.assert_called_once_with()
    follower.kill.assert_called_once_with()
    assert follower.wait.call_count == 2
    reader.join.assert_called_once_with(timeout=3)
    assert app.follow is None
    assert app.follow_thread is None


def test_shutdown_joins_real_log_consumer_and_supervisor(tmp_path):
    app = instance(tmp_path)
    command = [
        sys.executable,
        "-u",
        "-c",
        "import time; print('fixture service log', flush=True); time.sleep(30)",
    ]
    with patch.object(app, "command", return_value=command):
        app.start_logs()
        reader = app.follow_thread
        assert reader is not None
        assert reader.is_alive()

        supervisor = threading.Thread(target=app.monitor, daemon=True)
        app.monitor_thread = supervisor
        supervisor.start()
        assert supervisor.is_alive()

        app.shutdown()

    assert app.shutdown_event.is_set()
    assert app.follow is None
    assert app.follow_thread is None
    assert not reader.is_alive()
    assert not supervisor.is_alive()


def test_start_logs_is_disabled_after_launcher_shutdown(tmp_path):
    app = instance(tmp_path)
    app.shutdown_event.set()

    with patch.object(runtime.subprocess, "Popen") as popen:
        app.start_logs()

    popen.assert_not_called()


def test_launcher_chat_shortcut_keeps_focus_request(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/ai?focus=chat")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/ai/assets/ai.html?focus=chat"
        response.read()
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
            "telegram-worker",
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
        for service in runtime.REQUIRED_SERVICES - {"intelligence-worker"}
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



def test_workspace_probe_uses_authenticated_control_plane_readiness(tmp_path):
    app = instance(tmp_path)
    connection = MagicMock()
    connection.getresponse.return_value.status = 200

    with patch.object(runtime.http.client, "HTTPConnection", return_value=connection):
        assert app.probe_workspace() is True

    connection.request.assert_called_once_with(
        "GET",
        "/v1/health/workspace",
        headers={
            "Authorization": (
                "Bearer " + app.values["KATCHA_CONTROL_API_TOKEN"]
            )
        },
    )


def test_workspace_probe_preserves_anonymous_development_fallback(tmp_path):
    app = instance(tmp_path)
    app.values["KATCHA_CONTROL_API_TOKEN"] = ""
    connection = MagicMock()
    connection.getresponse.return_value.status = 200

    with patch.object(runtime.http.client, "HTTPConnection", return_value=connection):
        assert app.probe_workspace() is True

    connection.request.assert_called_once_with(
        "GET",
        "/v1/health/workspace",
        headers={},
    )


def test_launcher_serves_workspace_shell_without_api(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/home")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/home/assets/home.html"
        response.read()
        connection.request("GET", "/home/assets/home.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'What needs you now.' in body
        connection.request("GET", "/pages/home-aerith.css")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/css")
        assert b'.home-hero' in body
        connection.request("GET", "/home.js")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith(
            ("text/javascript", "application/javascript")
        )
        assert b'loadChannel' in body
        connection.request("GET", "/explorer")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/explorer/assets/index.html"
        response.read()
        connection.request("GET", "/explorer/assets/index.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'href="/styles.css"' in body
        assert b'href="/pages/trends-aerith.css"' in body
        assert b'src="/explorer.js"' in body
        for asset in (
            "/styles.css",
            "/explorer-refresh.css",
            "/system/aerith-base.css",
            "/pages/trends-aerith.css",
        ):
            connection.request("GET", asset)
            response = connection.getresponse()
            asset_body = response.read()
            assert response.status == 200, asset
            assert response.getheader("Content-Type").startswith("text/css"), asset
            assert asset_body, asset
        connection.request("GET", "/launcher-bridge.js")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'installWorkspaceMenu' in body
        connection.request("GET", "/editing")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/editing/assets/editing.html"
        response.read()
        connection.request("GET", "/editing/assets/editing.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"Production" in body
        connection.request("GET", "/clips")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/editing/assets/clips.html"
        response.read()
        connection.request("GET", "/editing/assets/clips.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'data-katcha-page="clips"' in body
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
        connection.request("GET", "/studio?channel=channel-1&episode=episode-1")
        response = connection.getresponse()
        assert response.status == 302
        assert (
            response.getheader("Location")
            == "/studio/assets/studio.html?channel=channel-1&episode=episode-1"
        )
        response.read()
        connection.request("GET", "/studio/assets/studio.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'data-katcha-page="studio"' in body
        connection.request("GET", "/system/aerith-shell.css")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"#katcha-chat-shortcut" in body
        connection.request("GET", "/pages/production-aerith.css")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b'data-katcha-page="production"' in body
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
        connection.request("GET", "/settings")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == "/settings/assets/settings.html"
        response.read()
        connection.request("GET", "/settings/assets/settings.html")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert b"Continue with ChatGPT" in body
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_workspace_css_mime_does_not_depend_on_host_mime_database(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch.object(runtime.mimetypes, "guess_type", return_value=(None, None)):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            for asset in (
                "/styles.css",
                "/pages/trends-aerith.css",
                "/explorer/assets/styles.css",
            ):
                connection.request("GET", asset)
                response = connection.getresponse()
                response.read()
                assert response.status == 200, asset
                assert response.getheader("Content-Type") == "text/css; charset=utf-8", asset
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


def test_apply_ai_recreates_only_ai_consumers(tmp_path):
    app = instance(tmp_path)
    app.desired_running = True
    app.phase = "ready"
    with (
        patch.object(app, "run") as run,
        patch.object(app, "probe_workspace", return_value=True),
        patch.object(app, "start_logs"),
    ):
        assert app.apply_ai() is True

    command = run.call_args.args[0]
    assert "--force-recreate" in command
    for service in (
        "api",
        "worker",
        "analysis-worker",
        "production-worker",
        "intelligence-worker",
    ):
        assert service in command
    assert "renderer" not in command
    assert "telegram-worker" not in command


def test_apply_telegram_recreates_only_api_and_telegram_worker(tmp_path):
    app = instance(tmp_path)
    app.desired_running = True
    app.phase = "ready"
    app.save(
        {
            "KATCHA_TELEGRAM_ENABLED": "true",
            "KATCHA_TELEGRAM_BOT_TOKEN": "123456:fixture-bot-token",
        }
    )
    with (
        patch.object(app, "run") as run,
        patch.object(app, "probe_workspace", return_value=True),
        patch.object(app, "start_logs"),
    ):
        assert app.apply_telegram() is True

    command = run.call_args.args[0]
    assert "--force-recreate" in command
    assert "api" in command
    assert "telegram-worker" in command
    assert "production-worker" not in command
    assert "renderer" not in command


def test_steady_state_monitor_bounds_workspace_probe_frequency(tmp_path):
    app = instance(tmp_path)
    app.phase = "ready"
    app.health_check_at = runtime.time.monotonic() + 100
    app.workspace_check_at = runtime.time.monotonic() + 100
    with patch.object(app, "probe_workspace") as probe:
        app.monitor_once()
        probe.assert_not_called()
        app.workspace_check_at = 0
        app.monitor_once()
        probe.assert_called_once()


def test_launcher_studio_home_and_shared_styles_are_local_and_keep_context(tmp_path):
    app = instance(tmp_path)
    server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.Handler)
    server.runtime = app
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        for path, target in [
            (
                "/studio?channel=one&episode=two",
                "/studio/assets/studio.html?channel=one&episode=two",
            ),
            ("/home", "/home/assets/home.html"),
        ]:
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 302
            assert response.getheader("Location") == target
            response.read()
        for path in [
            "/system/aerith-components.css", "/system/aerith-shell.css",
            "/studio/assets/studio.html", "/home/assets/home.html",
        ]:
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_workspace_recovers_before_next_container_health_check(tmp_path):
    app = instance(tmp_path)
    app.phase = "degraded"
    app.desired_running = True
    app.services = [{"Service": name, "State": "running"} for name in runtime.REQUIRED_SERVICES]
    app.health_check_at = runtime.time.monotonic() + 100

    def recovered():
        app.workspace_ready = True
        return True

    with (
        patch.object(app, "probe_workspace", side_effect=recovered) as probe,
        patch.object(app, "check") as check,
        patch.object(app, "operate") as operate,
    ):
        app.monitor_once()
    assert app.snapshot()["workspace_ready"] is True
    probe.assert_called_once()
    check.assert_not_called()
    operate.assert_not_called()


def test_replaced_pollers_stop_only_within_katcha_project(tmp_path):
    app = instance(tmp_path)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "ps" in args and args[-1].endswith("=discovery-worker"):
            return "old-discovery-1\nold-discovery-2\n"
        return ""

    with patch.object(app, "run", side_effect=fake_run):
        app.stop_retired_workers()
    searches = [call for call in calls if "ps" in call]
    assert len(searches) == 4
    assert all("label=com.docker.compose.project=katcha" in call for call in searches)
    assert [call for call in calls if "stop" in call] == [
        ["docker", "stop", "--time", "30", "old-discovery-1", "old-discovery-2"]
    ]
    assert not any("rm" in call or "down" in call for call in calls)


@pytest.mark.parametrize("retired_state", ["running", "exited"])
def test_reattach_ignores_retired_worker_health(tmp_path, retired_state):
    app = instance(tmp_path)
    rows = [
        {"Service": service, "State": "running", "Health": "healthy", "ExitCode": 0}
        for service in runtime.REQUIRED_SERVICES
    ]
    rows.append({"Service": "discovery-worker", "State": retired_state, "ExitCode": 1})
    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    with (
        patch.object(app, "run", return_value=json.dumps(rows)),
        patch.object(app, "stop_retired_workers") as retire,
        patch.object(app, "start_logs"),
        patch.object(runtime.http.client, "HTTPConnection", return_value=connection),
    ):
        assert app.reconcile_existing()
    assert retire.call_count == int(retired_state == "running")
    assert app.phase == "ready"
    assert not any(row["Service"] == "discovery-worker" for row in app.services)
