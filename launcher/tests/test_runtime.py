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
