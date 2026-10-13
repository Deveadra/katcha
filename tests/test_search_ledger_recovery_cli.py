"""Offline CLI wiring spies; these tests do not establish PostgreSQL recovery."""

import json
import socket
import sys
from pathlib import Path

import pytest

from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.postgres_jobs import (
    PostgresSearchJobLedger,
    SearchLedgerRecoveryError,
)
from katcha.ops import search_ledger_recovery as cli

OPERATIONS = {
    "inspect": "inspect_postgres_ledger",
    "quarantine": "quarantine_postgres_ledger",
    "import-sqlite": "import_sqlite_ledger",
    "activate": "activate_postgres_ledger",
}


@pytest.fixture
def offline_cli(monkeypatch):
    engine = object()
    calls = []
    monkeypatch.setattr(cli, "engine", engine)

    def prohibited(*args, **kwargs):
        pytest.fail("Offline recovery CLI must not dispatch, bootstrap SQLite or contact providers")

    monkeypatch.setattr(SearchJobLedger, "__init__", prohibited)
    monkeypatch.setattr(SearchJobLedger, "dispatch", prohibited)
    monkeypatch.setattr(PostgresSearchJobLedger, "dispatch", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "socket", prohibited)
    for function in OPERATIONS.values():
        monkeypatch.setattr(cli, function, prohibited)

    def run(arguments):
        monkeypatch.setattr(sys, "argv", ["search_ledger_recovery", *arguments])
        cli.main()

    return engine, calls, run


@pytest.mark.parametrize("command", ["inspect", "quarantine"])
def test_read_and_quarantine_commands_call_only_the_selected_operation(
    command, offline_cli, monkeypatch, capsys
):
    engine, calls, run = offline_cli
    report = {"ledger_id": "retained-id", "recovery_required": command == "quarantine"}

    def operation(received):
        assert received is engine
        calls.append(command)
        return report

    monkeypatch.setattr(cli, OPERATIONS[command], operation)
    run([command])
    assert calls == [command]
    assert json.loads(capsys.readouterr().out) == report


def test_import_wires_exact_source_generation_and_confirmed_quiescence(
    offline_cli, monkeypatch, tmp_path, capsys
):
    engine, calls, run = offline_cli
    source = tmp_path / "retained.sqlite"
    source.write_bytes(b"retained historical source; spy does not read it")

    def operation(received, path, **kwargs):
        assert received is engine
        assert path == source and isinstance(path, Path)
        assert kwargs == {"recovery_generation": 8, "confirmed_quiesced": True}
        calls.append("import")
        return {"ledger_id": "old-id", "recovery_required": True}

    monkeypatch.setattr(cli, "import_sqlite_ledger", operation)
    run(
        ["import-sqlite", "--source", str(source), "--generation", "8", "--confirm-workers-stopped"]
    )
    assert calls == ["import"]
    assert json.loads(capsys.readouterr().out)["recovery_required"] is True
    assert source.read_bytes() == b"retained historical source; spy does not read it"


def activation_arguments():
    return [
        "activate",
        "--expected-identity",
        "reviewed-id",
        "--expected-seal",
        "reviewed-seal",
        "--generation",
        "9",
        "--expected-generation",
        "8",
        "--completeness-record",
        "durable-review:backup-to-loss-interval",
    ]


def test_activation_wires_reviewed_identity_seal_generation_and_attestation(
    offline_cli, monkeypatch, capsys
):
    engine, calls, run = offline_cli

    def operation(received, **kwargs):
        assert received is engine
        assert kwargs == {
            "expected_identity": "reviewed-id",
            "expected_seal": "reviewed-seal",
            "recovery_generation": 9,
            "expected_generation": 8,
            "completeness_evidence": "durable-review:backup-to-loss-interval",
            "confirmed_quiesced": True,
        }
        calls.append("activate")
        return {"recovery_generation": 9, "recovery_required": False}

    monkeypatch.setattr(cli, "activate_postgres_ledger", operation)
    run([*activation_arguments(), "--confirm-workers-stopped"])
    assert calls == ["activate"]
    assert json.loads(capsys.readouterr().out) == {
        "recovery_generation": 9,
        "recovery_required": False,
    }


@pytest.mark.parametrize("command", ["import-sqlite", "activate"])
def test_missing_worker_confirmation_is_passed_false_and_rejection_cannot_reset_evidence(
    command, offline_cli, monkeypatch, tmp_path, capsys
):
    engine, calls, run = offline_cli
    source = tmp_path / "missing.sqlite"

    def operation(received, *args, **kwargs):
        assert received is engine
        assert kwargs["confirmed_quiesced"] is False
        calls.append(command)
        raise SearchLedgerRecoveryError(
            "Quiesced workers are required; retained evidence unchanged"
        )

    monkeypatch.setattr(cli, OPERATIONS[command], operation)
    arguments = (
        activation_arguments()
        if command == "activate"
        else [
            "import-sqlite",
            "--source",
            str(source),
            "--generation",
            "8",
        ]
    )
    with pytest.raises(SystemExit) as exc:
        run(arguments)
    assert exc.value.code == 2
    assert calls == [command]
    assert not source.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Search evidence recovery refused:" in captured.err


@pytest.mark.parametrize("command", ["inspect", "quarantine", "import-sqlite", "activate"])
def test_recovery_failure_never_falls_back_initializes_or_prints_success(
    command, offline_cli, monkeypatch, tmp_path, capsys
):
    _, calls, run = offline_cli

    def operation(*args, **kwargs):
        calls.append(command)
        raise SearchLedgerRecoveryError("Evidence missing or incomplete; dispatch remains closed")

    monkeypatch.setattr(cli, OPERATIONS[command], operation)
    arguments = [command]
    if command == "import-sqlite":
        arguments += ["--source", str(tmp_path / "missing.sqlite"), "--generation", "8"]
    elif command == "activate":
        arguments = activation_arguments()
    with pytest.raises(SystemExit) as exc:
        run(arguments)
    assert exc.value.code == 2
    assert calls == [command]
    assert list(tmp_path.iterdir()) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dispatch remains closed" in captured.err


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["dispatch"],
        ["inspect", "--force"],
        ["import-sqlite", "--generation", "8"],
        ["activate", "--expected-identity", "id"],
    ],
)
def test_missing_required_or_execution_bypass_arguments_fail_before_any_operation(
    arguments, offline_cli
):
    _, calls, run = offline_cli
    with pytest.raises(SystemExit) as exc:
        run(arguments)
    assert exc.value.code == 2
    assert calls == []


@pytest.mark.parametrize("error", [RuntimeError, ValueError, OSError])
def test_unexpected_failures_never_expose_secret_connection_or_sql_details(
    error, offline_cli, monkeypatch, capsys
):
    _, calls, run = offline_cli

    def operation(*args, **kwargs):
        calls.append("inspect")
        raise error("postgresql://private-user:secret@private-host; SQL payload-sensitive")

    monkeypatch.setattr(cli, "inspect_postgres_ledger", operation)
    with pytest.raises(SystemExit) as exc:
        run(["inspect"])
    assert exc.value.code == 2
    assert calls == ["inspect"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Search evidence recovery failed; keep dispatch disabled and inspect retained evidence.\n"
    )
    assert all(
        value not in captured.err for value in ("secret", "private-host", "payload-sensitive")
    )


@pytest.mark.parametrize("pre_identity", [False, True])
def test_legacy_import_flags_forward_only_explicit_reviewed_exceptions(
    pre_identity, offline_cli, monkeypatch, tmp_path, capsys
):
    engine, calls, run = offline_cli
    source = tmp_path / "historical.sqlite"
    jobs = ("ssj_" + "a" * 64, "ssj_" + "b" * 64)

    def operation(received, path, **kwargs):
        assert received is engine and path == source
        expected = {
            "recovery_generation": 8,
            "confirmed_quiesced": True,
            "legacy_evidence_ref": "review:pre-candidate-history",
            "legacy_metadata_gap_job_ids": jobs,
        }
        if pre_identity:
            expected["allow_pre_identity"] = True
        assert kwargs == expected
        calls.append("legacy-import")
        return {
            "recovery_required": True,
            "metadata_gaps": [
                {"job_id": job, "category": "legacy_candidate_metadata_unavailable"} for job in jobs
            ],
            "identity_assigned_for_pre_identity": pre_identity,
        }

    monkeypatch.setattr(cli, "import_sqlite_ledger", operation)
    arguments = [
        "import-sqlite",
        "--source",
        str(source),
        "--generation",
        "8",
        "--confirm-workers-stopped",
        "--legacy-evidence-ref",
        " review:pre-candidate-history ",
        "--legacy-metadata-gap-job-id",
        jobs[0],
        "--legacy-metadata-gap-job-id",
        jobs[1],
    ]
    if pre_identity:
        arguments.append("--allow-pre-identity")
    run(arguments)
    assert calls == ["legacy-import"]
    report = json.loads(capsys.readouterr().out)
    assert report["recovery_required"] is True
    assert report["metadata_gaps"] == [
        {"job_id": job, "category": "legacy_candidate_metadata_unavailable"} for job in jobs
    ]
    assert report["identity_assigned_for_pre_identity"] is pre_identity
    assert not source.exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--allow-pre-identity"],
        ["--legacy-metadata-gap-job-id", "ssj_" + "a" * 64],
        ["--legacy-evidence-ref", "  "],
        [
            "--legacy-evidence-ref",
            "review:record",
            "--legacy-metadata-gap-job-id",
            "same-job",
            "--legacy-metadata-gap-job-id",
            "same-job",
        ],
    ],
)
def test_legacy_exception_without_exact_review_declaration_stops_before_import(extra, offline_cli):
    _, calls, run = offline_cli
    with pytest.raises(SystemExit) as exc:
        run(["import-sqlite", "--source", "retained.sqlite", "--generation", "8", *extra])
    assert exc.value.code == 2
    assert calls == []


def test_review_reference_alone_does_not_enable_missing_identity_or_metadata(
    offline_cli, monkeypatch, capsys
):
    engine, calls, run = offline_cli

    def operation(received, path, **kwargs):
        assert received is engine
        assert kwargs == {
            "recovery_generation": 8,
            "confirmed_quiesced": False,
            "legacy_evidence_ref": "review:retained-history",
        }
        calls.append("import")
        raise SearchLedgerRecoveryError("Legacy metadata exception was not explicitly declared")

    monkeypatch.setattr(cli, "import_sqlite_ledger", operation)
    with pytest.raises(SystemExit) as exc:
        run(
            [
                "import-sqlite",
                "--source",
                "retained.sqlite",
                "--generation",
                "8",
                "--legacy-evidence-ref",
                "review:retained-history",
            ]
        )
    assert exc.value.code == 2
    assert calls == ["import"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not explicitly declared" in captured.err
