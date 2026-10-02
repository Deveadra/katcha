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
            "Health": {"Status": "unhealthy", "Log": ["private-value"]},
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
