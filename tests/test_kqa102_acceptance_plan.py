"""Offline acceptance planning must never turn declarations into live execution."""

import copy
import importlib.util
import json
import socket
import subprocess
import uuid
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "kqa102_acceptance_plan",
    Path(__file__).resolve().parents[1] / "scripts/kqa102_acceptance_plan.py",
)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def declaration():
    channel, project = str(uuid.uuid4()), str(uuid.uuid4())
    return {
        "format_version": 1,
        "channel_id": channel,
        "project_id": project,
        "revision": 2,
        "script_sha256": "a" * 64,
        "release_sha": "b" * 40,
        "operator": "acceptance-operator",
        "authorization_ref": None,
        "sources": [
            {
                "source_id": "owned-footage",
                "channel_id": channel,
                "project_id": project,
                "source_class": "owned",
                "sha256": "c" * 64,
                "permission_evidence_refs": ["rights:fixture"],
                "inspection_authorized": True,
                "acquisition_authorized": False,
                "production_authorized": True,
                "permission_expires_at": None,
            }
        ],
        "providers": [
            {
                "key": "example",
                "version": "v1",
                "max_calls": 3,
                "max_results": 20,
                "max_cost_microdollars": 0,
            }
        ],
    }


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("offline plan attempted network or subprocess work")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


def test_authorization_reference_never_opens_live_gate(declaration):
    declaration["authorization_ref"] = "authorization:review-request"
    plan = runner.prepare_plan(declaration)
    assert plan["status"] == "offline_prepared"
    assert plan["provider_calls_executed"] == 0
    assert not plan["live_execution_enabled"]
    assert not plan["declarations_verified"]
    assert not plan["production_acceptance"]
    assert "KQA-101" in plan["next_action"]
    assert "unknown" in plan["recovery_rule"]
    assert any("final compile/render" in step for step in plan["required_before_live"])


def test_plan_identity_freezes_scope_rights_and_caps(declaration):
    first = runner.prepare_plan(declaration)
    assert first == runner.prepare_plan(copy.deepcopy(declaration))
    for change in ("revision", "cap", "rights"):
        altered = copy.deepcopy(declaration)
        if change == "revision":
            altered["revision"] += 1
        elif change == "cap":
            altered["providers"][0]["max_calls"] += 1
        else:
            altered["sources"][0]["production_authorized"] = False
        assert runner.prepare_plan(altered)["plan_sha256"] != first["plan_sha256"]
    first["scope_and_declarations"]["sources"][0]["source_id"] = "changed"
    assert declaration["sources"][0]["source_id"] == "owned-footage"


@pytest.mark.parametrize("field", ["channel_id", "project_id"])
def test_cross_channel_or_project_source_denied(declaration, field):
    declaration["sources"][0][field] = str(uuid.uuid4())
    with pytest.raises(ValueError, match="another channel/project"):
        runner.prepare_plan(declaration)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_calls", True),
        ("max_calls", 0),
        ("max_calls", 1001),
        ("max_results", -1),
        ("max_cost_microdollars", 0.1),
        ("max_cost_microdollars", -1),
        ("max_cost_microdollars", float("inf")),
    ],
)
def test_invalid_unbounded_or_fractional_budgets_rejected(declaration, field, value):
    declaration["providers"][0][field] = value
    with pytest.raises(ValueError):
        runner.prepare_plan(declaration)


@pytest.mark.parametrize("change", ["reference", "no_evidence", "secret", "url", "expiry"])
def test_rights_and_secret_declarations_rejected(declaration, change):
    source = declaration["sources"][0]
    if change == "reference":
        source["source_class"] = "reference_only"
    elif change == "no_evidence":
        source["permission_evidence_refs"] = []
    elif change == "secret":
        declaration["providers"][0]["api_key"] = "must-never-be-exported"
    elif change == "url":
        source["permission_evidence_refs"] = ["https://example.invalid/?token=secret"]
    else:
        source["permission_expires_at"] = "2026-10-12T10:00:00"
    with pytest.raises(ValueError):
        runner.prepare_plan(declaration)


def test_cli_report_is_offline_and_never_overwrites_input_or_existing_report(tmp_path, declaration):
    source = tmp_path / "input.json"
    destination = tmp_path / "plan.json"
    original = json.dumps(declaration)
    source.write_text(original)
    assert runner.main(["--input", str(source), "--output", str(destination)]) == 0
    report = destination.read_bytes()
    assert json.loads(report)["provider_calls_executed"] == 0
    for output in (source, destination):
        with pytest.raises(SystemExit) as exc:
            runner.main(["--input", str(source), "--output", str(output)])
        assert exc.value.code == 2
    assert source.read_text() == original
    assert destination.read_bytes() == report


@pytest.mark.parametrize(
    "payload",
    ['{"format_version":1,"format_version":1}', "x" * 1_048_577],
    ids=["duplicate-keys", "oversize-json"],
)
def test_ambiguous_or_oversize_json_creates_no_report(tmp_path, payload):
    source, destination = tmp_path / "input.json", tmp_path / "report.json"
    source.write_text(payload)
    with pytest.raises(SystemExit) as exc:
        runner.main(["--input", str(source), "--output", str(destination)])
    assert exc.value.code == 2
    assert not destination.exists()


def test_no_execute_or_authorization_bypass_cli(tmp_path):
    with pytest.raises(SystemExit) as exc:
        runner.main(["--execute", "--input", str(tmp_path / "x"), "--output", str(tmp_path / "y")])
    assert exc.value.code == 2


@pytest.mark.parametrize("field", ["sources", "providers"])
def test_duplicate_plan_operations_rejected(declaration, field):
    declaration[field].append(copy.deepcopy(declaration[field][0]))
    with pytest.raises(ValueError, match="duplicate"):
        runner.prepare_plan(declaration)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_class", []),
        ("inspection_authorized", "yes"),
        ("sha256", "unmeasured"),
        ("permission_expires_at", 42),
    ],
)
def test_malformed_source_fields_fail_cleanly(declaration, field, value):
    declaration["sources"][0][field] = value
    with pytest.raises(ValueError):
        runner.prepare_plan(declaration)
