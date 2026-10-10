"""Offline planning cannot mistake compute slots for storage or authorization."""
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "probe_plan", Path("scripts/plan-oci-identity-probe.py")
)
assert SPEC and SPEC.loader
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)
NOW = datetime(2026, 10, 8, 18, tzinfo=UTC)


def evidence():
    return {
        **dict.fromkeys(plan.CHECKS, True),
        **dict.fromkeys(plan.SWITCHES, False),
        "observed_at": NOW.isoformat(),
        "micro_slots_remaining": 2,
        "storage_remaining_gb": 50,
        "image_minimum_boot_gb": 50,
        "planned_boot_gb": 50,
        "shape": plan.SHAPE,
        "availability_domain": "TEST:AD-3",
        "shape_availability_domains": ["TEST:AD-3"],
        "attach_production_volume": False,
        "assign_public_ip": False,
        "production_bootstrap": False,
        "additional_data_gb": 0,
    }


def test_two_slots_allow_only_one_50gb_probe_and_never_authorize():
    result = plan.evaluate(evidence(), now=NOW)
    assert result["status"] == "PLAN_REVIEWABLE"
    assert result["planned_instances"] == 1
    assert result["storage_after_probe_gb"] == 0
    for key in ("launch_authorized", "secret_access_authorized", "recovery_acceptance_complete"):
        assert result[key] is False


@pytest.mark.parametrize("key", list(evidence()))
def test_every_evidence_field_is_required(key):
    data = evidence()
    del data[key]
    assert plan.evaluate(data, now=NOW)["status"] == "BLOCKED"


@pytest.mark.parametrize("key,value", [
    ("micro_slots_remaining", 0), ("micro_slots_remaining", 1.5),
    ("storage_remaining_gb", 49), ("storage_remaining_gb", float("nan")),
    ("storage_remaining_gb", float("inf")), ("planned_boot_gb", True),
    ("planned_boot_gb", 49), ("planned_boot_gb", 51),
    ("image_minimum_boot_gb", 60), ("image_minimum_boot_gb", 0),
    ("additional_data_gb", 50), ("additional_data_gb", False),
    ("attach_production_volume", True), ("assign_public_ip", True),
    ("production_bootstrap", True), ("shape", "VM.Standard.A1.Flex"),
    ("availability_domain", "TEST:AD-1"), ("shape_availability_domains", "TEST:AD-3"),
    ("KATCHA_OCI_RECOVERY_CONFIGURED", "false"), ("billing_reviewed", "true"),
    ("observed_at", (NOW - timedelta(hours=2)).isoformat()),
    ("observed_at", (NOW + timedelta(seconds=1)).isoformat()),
    ("observed_at", "2026-10-08T18:00:00"),
])
def test_unsafe_or_unverified_plan_is_blocked(key, value):
    data = evidence()
    data[key] = value
    assert plan.evaluate(data, now=NOW)["status"] == "BLOCKED"


def test_untrusted_extra_approval_field_does_not_authorize():
    data = evidence()
    data["launch_authorized"] = True
    assert plan.evaluate(data, now=NOW)["launch_authorized"] is False


def test_cli_malformed_input_is_redacted(tmp_path, monkeypatch, capsys):
    source = tmp_path / "evidence.json"
    source.write_text("private-text-not-json")
    monkeypatch.setattr("sys.argv", ["plan", str(source)])
    assert plan.main() == 2
    assert capsys.readouterr().out == '{"status":"INVALID_EVIDENCE"}\n'


@pytest.mark.parametrize("valid", [False, True])
def test_cli_exit_status_never_treats_missing_evidence_as_success(
    valid, tmp_path, monkeypatch, capsys
):
    data = evidence() if valid else {}
    data["observed_at"] = datetime.now(UTC).isoformat()
    source = tmp_path / "metadata.json"
    source.write_text(json.dumps(data))
    monkeypatch.setattr("sys.argv", ["plan", str(source)])
    assert plan.main() == (0 if valid else 2)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == ("PLAN_REVIEWABLE" if valid else "BLOCKED")
    assert result["launch_authorized"] is False
