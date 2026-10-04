import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "diagnose_runtime", Path(__file__).resolve().parents[2] / "scripts" / "diagnose_runtime.py"
)
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


def test_diagnostics_exports_resource_evidence_without_credentials():
    row = {
        "Config": {
            "Env": ["SECRET=private-value"],
            "Labels": {"com.docker.compose.service": "api"},
        },
        "State": {
            "Status": "exited",
            "OOMKilled": True,
            "ExitCode": 137,
            "Health": {
                "Status": "unhealthy",
                "FailingStreak": 4,
                "Log": ["private-value"],
            },
        },
        "RestartCount": 3,
        "HostConfig": {"Memory": 1024, "NanoCpus": 1000000000},
    }
    with (
        patch.object(diagnostics.urllib.request, "urlopen", side_effect=OSError("unavailable")),
        patch.object(
            diagnostics, "docker", side_effect=["abc", json.dumps([row]), '{"CPUPerc":"99%"}']
        ),
    ):
        result = diagnostics.collect()
    assert result["containers"][0]["state"]["OOMKilled"] is True
    assert result["containers"][0]["restart_count"] == 3
    assert result["containers"][0]["health_failing_streak"] == 4
    assert result["resource_usage"][0]["CPUPerc"] == "99%"
    assert "private-value" not in json.dumps(result)


def test_diagnostics_reports_unavailable_docker_without_raw_errors():
    with (
        patch.object(diagnostics.urllib.request, "urlopen", side_effect=OSError("private-value")),
        patch.object(diagnostics, "docker", side_effect=FileNotFoundError("private-value")),
    ):
        result = diagnostics.collect()
    assert result["docker"]["available"] is False
    assert result["launcher"]["available"] is False
    assert "private-value" not in json.dumps(result)


def test_postgres_probe_is_read_only_and_exports_aggregate_evidence():
    row = {
        "Id": "postgres-id",
        "Config": {"Labels": {"com.docker.compose.service": "postgres"}},
        "State": {"Status": "running"},
    }
    with patch.object(diagnostics, "docker", return_value='{"lock_waiters":2}') as run:
        result = diagnostics.postgres_snapshot([row])
    assert result == {"available": True, "lock_waiters": 2}
    args = run.call_args.args
    assert args[:2] == ("exec", "postgres-id")
    assert "-X" in args
    assert "ON_ERROR_STOP=1" in args
    sql = args[-1]
    assert "BEGIN READ ONLY" in sql
    assert "statement_timeout = '3s'" in sql
    assert "ROLLBACK" in sql
    assert "query," not in sql


def test_postgres_probe_failure_preserves_resource_snapshot_and_hides_error():
    row = {
        "Id": "postgres-id",
        "Config": {"Labels": {"com.docker.compose.service": "postgres"}},
        "State": {"Status": "running"},
    }
    with (
        patch.object(diagnostics.urllib.request, "urlopen", side_effect=OSError("unavailable")),
        patch.object(
            diagnostics,
            "docker",
            side_effect=[
                "postgres-id",
                json.dumps([row]),
                '{"CPUPerc":"99%"}',
                TimeoutError("private-value"),
            ],
        ),
    ):
        result = diagnostics.collect()
    assert result["postgres"]["available"] is False
    assert result["resource_usage"][0]["CPUPerc"] == "99%"
    assert "private-value" not in json.dumps(result)


def test_postgres_probe_skips_stopped_or_absent_container():
    with patch.object(diagnostics, "docker") as run:
        assert diagnostics.postgres_snapshot([])["available"] is False
    run.assert_not_called()



def test_docker_event_snapshot_keeps_lifecycle_evidence_without_arbitrary_labels():
    event = {
        "time": 1791122400,
        "timeNano": 1791122400000000000,
        "Action": "die",
        "Actor": {
            "Attributes": {
                "com.docker.compose.service": "intelligence-worker",
                "exitCode": "137",
                "signal": "9",
                "private-token": "must-not-export",
            }
        },
    }
    with patch.object(diagnostics, "docker", return_value=json.dumps(event)) as run:
        result = diagnostics.docker_event_snapshot("2026-10-04T15:00:00+00:00")

    assert result == [
        {
            "time": 1791122400,
            "time_nano": 1791122400000000000,
            "action": "die",
            "service": "intelligence-worker",
            "exit_code": "137",
            "signal": "9",
        }
    ]
    args = run.call_args.args
    assert args[:2] == ("events", "--since")
    assert "private-token" not in json.dumps(result)


def test_pressure_snapshot_is_numeric_and_does_not_export_other_text(tmp_path):
    pressure = tmp_path / "memory.pressure"
    pressure.write_text(
        "some avg10=0.50 avg60=0.25 avg300=0.10 total=1234\n"
        "full avg10=0.20 avg60=0.10 avg300=0.05 total=567\n"
    )

    assert diagnostics._pressure_snapshot(pressure) == {
        "some": {
            "avg10": 0.5,
            "avg60": 0.25,
            "avg300": 0.1,
            "total": 1234,
        },
        "full": {
            "avg10": 0.2,
            "avg60": 0.1,
            "avg300": 0.05,
            "total": 567,
        },
    }
