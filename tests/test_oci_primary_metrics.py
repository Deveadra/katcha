"""Offline, read-only OCI metric collection and error classification."""
from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SOURCE = Path("scripts/oci-primary-metrics.py")
SPEC = importlib.util.spec_from_file_location("oci_primary_metrics", SOURCE)
assert SPEC and SPEC.loader
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)

INSTANCE = "ocid1.instance.oc1.iad.fixture"
COMPARTMENT = "ocid1.compartment.oc1..fixture"
NOW = datetime(2026, 10, 12, 19, 10, tzinfo=UTC)


def fixture_report(*, value=27.7, stamp=None, resource=INSTANCE):
    timestamp = (stamp or NOW - timedelta(minutes=5)).isoformat()
    return json.dumps({"data": [{
        "dimensions": {"resourceId": resource},
        "aggregated-datapoints": [{"timestamp": timestamp, "value": value}],
    }]})


def test_mql_exact_primary_and_unit_period():
    text = metrics.metric_query("MemoryUtilization", "mean", "5m", INSTANCE)
    assert text == f'MemoryUtilization[5m]{{resourceId = "{INSTANCE}"}}.mean()'
    assert metrics.metric_query("NetworksBytesOut", "rate", "1h", INSTANCE).endswith(
        '.rate()'
    )
    with pytest.raises(AssertionError):
        metrics.metric_query("Injected; delete", "rate", "1h", INSTANCE)


def test_read_only_query_has_fixed_scope_and_no_shell(monkeypatch):
    captured = []
    def fake_cmd(argv, *, seconds=40):
        captured.append(argv)
        return fixture_report()
    monkeypatch.setattr(metrics, "sanitized_command", fake_cmd)
    values = metrics.query_data(
        compartment=COMPARTMENT, instance=INSTANCE, metric="MemoryUtilization",
        stat="mean", period="5m", start=NOW - timedelta(hours=6), end=NOW,
    )
    assert values == [(NOW - timedelta(minutes=5), 27.7)]
    args = captured[0]
    assert args[args.index("--compartment-id") + 1] == COMPARTMENT
    assert args[args.index("--namespace") + 1] == "oci_computeagent"
    assert args[args.index("--resolution") + 1] == "5m"
    assert args[args.index("--output") + 1] == "json"
    assert args[args.index("--query-text") + 1] == (
        f'MemoryUtilization[5m]{{resourceId = "{INSTANCE}"}}.mean()'
    )
    assert "instance" not in args or "launch" not in args
    assert "bastion" not in args
    assert "secrets" not in args


def test_rejects_multiple_or_wrong_resource_streams(monkeypatch):
    monkeypatch.setattr(metrics, "sanitized_command", lambda *a, **k: json.dumps({
        "data": [{"dimensions": {"resourceId": "ocid1.instance.x"},
                  "aggregated-datapoints": []}],
    }))
    with pytest.raises(metrics.MetricsError, match="RESOURCE_ID_MISMATCH"):
        metrics.query_data(
            compartment=COMPARTMENT, instance=INSTANCE, metric="CpuUtilization",
            stat="mean", period="5m", start=NOW - timedelta(hours=6), end=NOW,
        )


@pytest.mark.parametrize("raw", [
    "Usage: oci monitoring...",
    '{"data":',
    '{"data":{"unexpected":"shape"}}',
    '{"data":[{"dimensions":{"resourceId":"foo"},"aggregated-datapoints":[]}]}',
])
def test_malformed_or_wrong_resource_does_not_become_success(monkeypatch, raw):
    monkeypatch.setattr(metrics, "sanitized_command", lambda *a, **k: raw)
    with pytest.raises(metrics.MetricsError):
        metrics.query_data(
            compartment=COMPARTMENT, instance=INSTANCE, metric="CpuUtilization",
            stat="mean", period="5m", start=NOW - timedelta(hours=6), end=NOW,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -2, 101, "27.7", True])
def test_invalid_utilization_data_is_never_reported(monkeypatch, value):
    monkeypatch.setattr(metrics, "sanitized_command",
                        lambda *a, **k: fixture_report(value=value))
    with pytest.raises(metrics.MetricsError, match="INVALID_OR_NON_JSON"):
        metrics.query_data(
            compartment=COMPARTMENT, instance=INSTANCE, metric="MemoryUtilization",
            stat="mean", period="5m", start=NOW - timedelta(hours=6), end=NOW,
        )


def test_gh_identifiers_must_be_verified(monkeypatch):
    monkeypatch.setattr(metrics, "OCI", Path("/tmp/fake-oci"))
    monkeypatch.setattr(metrics, "CONFIG", Path("/tmp/fake-config"))
    monkeypatch.setattr(metrics.Path, "is_file", lambda self: True)
    answers = iter(["", "ocid1.compartment.oc1..some", "not_an_instance"])
    monkeypatch.setattr(metrics, "sanitized_command", lambda *a, **k: next(answers))
    with pytest.raises(metrics.MetricsError, match="PRIMARY_INSTANCE_ID_NOT_VERIFIED"):
        metrics.preflight()


def test_oci_error_never_leaks_private_provider_output(monkeypatch, capsys):
    class Failed:
        returncode = 1
        stderr = "Error: NotAuthorizedOrNotFound PRIVATE_TOKEN=top_secret"
        stdout = "PRIVATE_TOKEN=top_secret"
    monkeypatch.setattr(metrics.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(metrics.MetricsError) as error:
        metrics.sanitized_command(["oci", "monitoring"])
    assert str(error.value) == "OCI_MONITORING_PERMISSION_DENIED"
    assert "top_secret" not in str(error.value)
    assert "top_secret" not in capsys.readouterr().out


def test_fresh_memory_window_and_seven_day_caveat(capsys):
    near, week = {}, {}
    for metric, _stat, unit in metrics.METRICS:
        value = 28.2 if unit == "%" else 1024.0
        near[metric] = [(NOW - timedelta(minutes=5), value)]
        week[metric] = [(NOW - timedelta(hours=1), value)]
    assert metrics.display(now=NOW, near=near, week=week) == 0
    out = capsys.readouterr().out
    assert "MemoryUtilization: latest=28.2%" in out
    assert "OCI_MONITORING_REPORT: COMPLETE" in out
    assert "not Oracle's seven-day CPU 95th percentile" in out
    assert "not a seven-day retention guarantee" in out


def test_stale_measurement_is_incomplete(capsys):
    near, week = {}, {}
    for metric, _, _unit in metrics.METRICS:
        near[metric] = [(NOW - timedelta(hours=1), 12.0)]
        week[metric] = [(NOW - timedelta(hours=1), 12.0)]
    assert metrics.display(now=NOW, near=near, week=week) == 2
    assert "WARNING: STALE_OR_FUTURE_DATAPOINT" in capsys.readouterr().out


def test_missing_measured_series_never_reports_complete(capsys):
    near = {metric: [] for metric, _, _unit in metrics.METRICS}
    week = {metric: [] for metric, _, _unit in metrics.METRICS}
    assert metrics.display(now=NOW, near=near, week=week) == 2
    assert "OCI_MONITORING_REPORT: INCOMPLETE" in capsys.readouterr().out


def test_admin_subcommand_uses_helper_file_and_inherited_io(monkeypatch):
    admin_source = Path("scripts/katcha-oci-admin.py").read_text()
    assert '"metrics"' in admin_source
    assert '"oci-primary-metrics.py"' in admin_source
    assert "check=False, timeout=360" in admin_source
