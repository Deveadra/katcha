from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from katcha.api.operations import operations_health
from katcha.ops import operations_health as ops
from katcha.services import operations_health as view

NOW = 1_792_800_000.0


@pytest.fixture
def ledger(tmp_path):
    with ops.connect(tmp_path) as db:
        yield db


def signals(status="critical", completed_at=None):
    return [
        ops.Signal("backup", "Backups", status, "Completion overdue", "Retry safely", completed_at),
        *[
            ops.Signal(key, key, "healthy", "Verified")
            for key in ("runtime", "storage", "authority")
        ],
    ]


def ready(monkeypatch, tmp_path, calls):
    monkeypatch.setattr(ops, "DEPLOY_LOCK", tmp_path / "deployment.lock")
    monkeypatch.setattr(
        ops,
        "properties",
        lambda unit: {
            "LoadState": "loaded",
            "ActiveState": "active" if unit.endswith("timer") else "inactive",
            "UnitFileState": "enabled",
            "Result": "success",
            "ExecMainStatus": "0",
        },
    )
    monkeypatch.setattr(ops, "command", lambda *args, **kw: calls.append(args) or "")


def test_incident_deduplicates_survives_reopen_and_records_recovery(tmp_path):
    with ops.connect(tmp_path) as db:
        ops.reconcile(db, signals(), NOW)
        ops.reconcile(db, signals(), NOW + 5)
    with ops.connect(tmp_path) as db:
        assert db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        ops.reconcile(db, signals("healthy"), NOW + 10)
        assert db.execute("SELECT bot FROM incidents").fetchone()[0] == "verified"
        ops.reconcile(db, signals(), NOW + 20)
        assert db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_bot_claims_before_side_effect_and_requires_new_completion(ledger, monkeypatch, tmp_path):
    calls = []
    ready(monkeypatch, tmp_path, calls)
    ops.reconcile(ledger, signals(), NOW)
    ops.remediate(ledger, NOW + 1)
    ops.remediate(ledger, NOW + 2)
    assert calls == [("systemctl", "start", "--no-block", "katcha-backup.service")]
    assert ledger.execute("SELECT bot FROM incidents").fetchone()[0] == "verifying"
    # A skipped unit returning success plus an old receipt must not verify remediation.
    ops.reconcile(ledger, signals("healthy", NOW - 10), NOW + 3)
    assert ledger.execute("SELECT resolved FROM incidents").fetchone()[0] is None
    ops.reconcile(ledger, signals("healthy", NOW + 4), NOW + 5)
    assert ledger.execute("SELECT bot FROM incidents").fetchone()[0] == "verified"


def test_unknown_start_outcome_is_never_retried(ledger, monkeypatch, tmp_path):
    calls = []
    ready(monkeypatch, tmp_path, calls)

    def uncertain(*args, **kwargs):
        calls.append(args)
        assert ledger.execute("SELECT attempts FROM incidents").fetchone()[0] == 1
        raise TimeoutError("SECRET provider output")

    monkeypatch.setattr(ops, "command", uncertain)
    ops.reconcile(ledger, signals(), NOW)
    ops.remediate(ledger, NOW + 1)
    ops.remediate(ledger, NOW + 5000)
    assert len(calls) == 1
    assert ledger.execute("SELECT bot FROM incidents").fetchone()[0] == "operator_decision"
    assert "SECRET" not in str([tuple(row) for row in ledger.execute("SELECT * FROM events")])


def test_bot_serializes_backup_and_restore_until_positive_completion(ledger, monkeypatch, tmp_path):
    calls = []
    ready(monkeypatch, tmp_path, calls)
    rows = signals() + [ops.Signal("restore", "Restores", "critical", "Overdue")]
    ops.reconcile(ledger, rows, NOW)
    ops.remediate(ledger, NOW)
    ops.remediate(ledger, NOW + 5)
    assert calls == [("systemctl", "start", "--no-block", "katcha-backup.service")]
    ops.reconcile(ledger, signals("healthy", NOW + 10) + rows[-1:], NOW + 11)
    ops.remediate(ledger, NOW + 12)
    assert calls[-1] == ("systemctl", "start", "--no-block", "katcha-restore-test.service")
    assert len(calls) == 2


@pytest.mark.parametrize("blocked", ["runtime", "storage", "authority"])
def test_bot_defers_without_independent_safety_evidence(ledger, monkeypatch, tmp_path, blocked):
    calls = []
    ready(monkeypatch, tmp_path, calls)
    ops.reconcile(ledger, [s for s in signals() if s.key != blocked], NOW)
    ops.remediate(ledger, NOW)
    assert not calls


def test_bot_does_not_race_deployment_or_an_active_safety_job(ledger, monkeypatch, tmp_path):
    import fcntl

    calls = []
    ready(monkeypatch, tmp_path, calls)
    ops.reconcile(ledger, signals(), NOW)
    with (tmp_path / "deployment.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ops.remediate(ledger, NOW)
    assert not calls
    monkeypatch.setattr(ops, "properties", lambda unit: {"ActiveState": "activating"})
    ops.remediate(ledger, NOW)
    assert not calls


def test_bot_requires_enabled_timer_and_escalates_unverified_retry(ledger, monkeypatch, tmp_path):
    calls = []
    ready(monkeypatch, tmp_path, calls)
    original = ops.properties
    monkeypatch.setattr(
        ops,
        "properties",
        lambda unit: {
            **original(unit),
            **({"UnitFileState": "disabled"} if unit.endswith("timer") else {}),
        },
    )
    ops.reconcile(ledger, signals(), NOW)
    ops.remediate(ledger, NOW)
    assert not calls
    assert ledger.execute("SELECT bot FROM incidents").fetchone()[0] == "operator_decision"
    ledger.execute("UPDATE incidents SET bot='verifying',attempted=?,attempts=1", (NOW,))
    ops.remediate(ledger, NOW + 3001)
    assert ledger.execute("SELECT bot FROM incidents").fetchone()[0] == "operator_decision"


def test_notification_outbox_retries_and_only_marks_confirmed_delivery(ledger, monkeypatch):
    calls = []
    failing = True

    def send(credentials, message):
        calls.append(message)
        if failing:
            raise RuntimeError("SECRET")

    monkeypatch.setattr(
        ops,
        "installed_notifier",
        lambda: SimpleNamespace(
            configuration=lambda: ("private-token", "operator", ""), telegram_send=send
        ),
    )
    ops.reconcile(ledger, signals(), NOW)
    ops.notify(ledger, NOW)
    assert ledger.execute("SELECT delivered FROM events").fetchone()[0] == 0
    failing = False
    ops.notify(ledger, NOW + 1)
    ops.notify(ledger, NOW + 2)
    assert len(calls) == 2
    assert ops.HEALTH_URL in calls[-1]
    assert "private-token" not in calls[-1]
    assert ledger.execute("SELECT delivered FROM events").fetchone()[0] == 1


@pytest.mark.parametrize("age,status", [(3600, "healthy"), (7201, "critical"), (-301, "unknown")])
def test_backup_positive_completion_age(monkeypatch, tmp_path, age, status):
    ready(monkeypatch, tmp_path, [])
    receipt = {
        "__REALTIME_TIMESTAMP": str(int((NOW - age) * 1_000_000)),
        "_SYSTEMD_UNIT": "katcha-backup.service",
        "MESSAGE": "Katcha PostgreSQL backup completed: 20261024T000000Z-abc",
    }
    monkeypatch.setattr(ops, "command", lambda *args, **kw: json.dumps(receipt))
    result = ops.safety_signal("backup", NOW)
    assert result.status == status


@pytest.mark.parametrize(
    "message",
    [
        "Katcha restore test already running; exiting.",
        "Katcha disaster restore test passed for 1 databases.",
    ],
)
def test_skipped_or_partial_restore_is_not_health_evidence(monkeypatch, tmp_path, message):
    ready(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        ops,
        "command",
        lambda *args, **kw: json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(int(NOW * 1_000_000)),
                "_SYSTEMD_UNIT": "katcha-restore-test.service",
                "MESSAGE": message,
            }
        ),
    )
    assert ops.safety_signal("restore", NOW).status == "unknown"


def test_active_jobs_do_not_substitute_for_evidence(monkeypatch, tmp_path):
    ready(monkeypatch, tmp_path, [])
    original = ops.properties
    monkeypatch.setattr(
        ops,
        "properties",
        lambda unit: {
            **original(unit),
            **({"ActiveState": "activating"} if unit.endswith("service") else {}),
        },
    )
    monkeypatch.setattr(ops, "command", lambda *args, **kw: "")
    assert ops.safety_signal("backup", NOW).status == "unknown"


@pytest.mark.parametrize(
    "bytes_free,inodes_free,status",
    [
        (1, 60, "critical"),
        (15, 90, "warning"),
        (80, 3, "critical"),
        (80, 10, "warning"),
        (80, 90, "healthy"),
    ],
)
def test_host_filesystem_checks_bytes_and_inodes(
    monkeypatch, bytes_free, inodes_free, status
):
    monkeypatch.setattr(
        ops.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=100, free=bytes_free),
    )
    monkeypatch.setattr(
        ops.os, "statvfs",
        lambda path: SimpleNamespace(f_blocks=100, f_files=100, f_favail=inodes_free),
    )
    result = ops.filesystem_signal("/", "root_storage", "Root filesystem")
    assert result.status == status
    assert "Root filesystem" in result.detail
    assert f"{inodes_free:.1f}% inodes free" in result.detail
    assert "SECRET" not in result.detail


def test_unreported_inodes_and_missing_capacity_fail_safe(monkeypatch):
    monkeypatch.setattr(
        ops.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=100, free=90),
    )
    monkeypatch.setattr(
        ops.os, "statvfs",
        lambda path: SimpleNamespace(f_blocks=100, f_files=0, f_favail=0),
    )
    row = ops.filesystem_signal("/", "root_storage", "Root filesystem")
    assert row.status == "healthy"
    assert "not reported" in row.detail
    monkeypatch.setattr(
        ops.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=0, free=0),
    )
    with pytest.raises(ops.ProbeError, match="could not be measured"):
        ops.filesystem_signal("/", "root_storage", "Root filesystem")


def test_docker_container_oom_is_actionable_without_host_private_paths(monkeypatch):
    names = ["katcha-production-worker-1", "katcha-production-renderer-1"]
    seen = []

    def fake_command(*args, **kwargs):
        seen.append(args)
        if args[:2] == ("docker", "ps"):
            return "\n".join(names)
        if args[:2] == ("docker", "inspect"):
            assert args[-2:] == tuple(names)
            assert ".Config" not in " ".join(args)
            return "7|true|exited\n0|false|running"
        raise AssertionError("unexpected Docker command")

    monkeypatch.setattr(ops, "command", fake_command)
    row = ops.production_container_pressure()
    assert row.status == "critical"
    assert "1 containers flagged OOM-killed" in row.detail
    assert "7 cumulative Docker restarts" in row.detail
    assert "worker-1" not in row.detail
    assert seen[-1][2] == "--format"


def test_docker_metadata_restarts_are_not_automatically_incidents(monkeypatch):
    def fake_command(*args, **kwargs):
        return (
            "katcha-production-api-1" if args[1] == "ps"
            else "15|false|running"
        )
    monkeypatch.setattr(ops, "command", fake_command)
    row = ops.production_container_pressure()
    assert row.status == "healthy"
    assert "15 cumulative" in row.detail


@pytest.mark.parametrize(
    "names,result",
    [
        ("katcha-production-api-1\nnot-allowed", "2|false|running\n0|false|running"),
        ("katcha-production-api-1", "error SECRET"),
        ("katcha-production-api-1", ""),
    ],
)
def test_docker_metadata_invalid_or_incomplete_is_unknown(monkeypatch, names, result):
    monkeypatch.setattr(
        ops, "command",
        lambda *args, **kwargs: names if args[1] == "ps" else result,
    )
    with pytest.raises(ops.ProbeError):
        ops.production_container_pressure()


def test_running_workflows_are_healthy_but_repeated_activity_failures_warn(monkeypatch):
    execution = {"workflowId": "scheduler", "runId": "run-1"}
    activities = []

    def command(*args, **kwargs):
        if any('ExecutionStatus="Failed"' in str(arg) for arg in args):
            return "[]"
        if "list" in args:
            return json.dumps([{"execution": execution}])
        return json.dumps(
            {"workflowExecutionInfo": {"execution": execution}, "pendingActivities": activities}
        )

    monkeypatch.setattr(ops, "command", command)
    assert ops.temporal_signal().status == "healthy"
    activities.append({"lastFailure": {"message": "SECRET"}, "attempt": 3})
    warning = ops.temporal_signal()
    assert warning.status == "warning"
    assert "SECRET" not in warning.detail



def test_temporal_failure_types_are_bounded_and_no_private_errors_leak(monkeypatch):
    rows = [
        {"type": {"name": "SourceIngestWorkflow"}, "execution": {"workflowId": "private-A"}},
        {"type": {"name": "SourceIngestWorkflow"}, "error": "SECRET"},
        {"type": {"name": "private:SECRET"}, "failure": {"message": "SECRET"}},
    ]

    def command(*args, **kwargs):
        if any('ExecutionStatus="Failed"' in str(arg) for arg in args):
            return json.dumps(rows)
        return "[]"

    monkeypatch.setattr(ops, "command", command)
    signal = ops.temporal_signal()
    assert signal.status == "warning"
    assert "3 failed executions" in signal.detail
    assert "SourceIngestWorkflow (2)" in signal.detail
    assert "unidentified (1)" in signal.detail
    assert "SECRET" not in signal.detail
    assert "private-A" not in signal.detail

    rows[:] = [{"type": {"name": "DiscoveryWorkflow"}}] * 101
    with pytest.raises(ops.ProbeError, match="incomplete"):
        ops.temporal_signal()


@pytest.mark.parametrize(
    "message,code,action_fragment",
    [
        ("OSError: [Errno 28] No space left on device", "DISK_FULL", "volume space"),
        ("Provider returned HTTP 429 Too Many Requests", "RATE_LIMIT", "provider quota"),
        ("upstream returned 401 unauthorized", "AUTHENTICATION", "credential"),
        ("HTTP 403 Forbidden", "PERMISSION", "access policy"),
        ("Connection refused by dependency", "NETWORK", "DNS"),
        ("ModuleNotFoundError: package_x", "DEPENDENCY_MISSING", "dependencies"),
        ("task timed out", "TIMEOUT", "capacity"),
        ("details for person@example.com", "UNCLASSIFIED", "private Temporal"),
    ],
)
def test_temporal_failure_codes_are_safe_and_actionable(message, code, action_fragment):
    failure = {
        "message": message,
        "stackTrace": "SECRET_STACK",
        "applicationFailureInfo": {"type": "SensitivePayload"},
    }
    assert ops.classify_temporal_failure(failure) == code
    assert action_fragment in ops.TEMPORAL_ACTIONS[code]
    assert message not in ops.TEMPORAL_ACTIONS[code]
    assert "SECRET" not in ops.TEMPORAL_ACTIONS[code]


def test_temporal_history_uses_terminal_event_and_nested_cause():
    history = {"events": [
        {"eventType": "EVENT_TYPE_WORKFLOW_TASK_FAILED",
         "workflowTaskFailedEventAttributes": {
             "failure": {"message": "old irrelevant failure"}}},
        {"eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED",
         "workflowExecutionFailedEventAttributes": {
             "failure": {"message": "workflow failed",
                         "cause": {"message": "HTTP 429: SECRET_KEY=123",
                                   "applicationFailureInfo": {"type": "RateLimited"}}}}},
    ]}
    assert ops.classify_temporal_history(history) == "RATE_LIMIT"
    assert ops.classify_temporal_history({"events": [
        {"workflowTaskFailedEventAttributes": {
            "cause": "WORKFLOW_TASK_FAILED_CAUSE_NON_DETERMINISTIC_ERROR",
            "failure": {"message": "SECRET"}}}
    ]}) == "CODE_COMPATIBILITY"
    assert ops.classify_temporal_history({"events": [
        {"workflowExecutionFailedEventAttributes": {
            "failure": {"timeoutFailureInfo": {"timeoutType": "START_TO_CLOSE"}}}}
    ]}) == "ACTIVITY_TIMEOUT"
    assert ops.classify_temporal_history({"events": []}) == "HISTORY_UNAVAILABLE"
    assert ops.classify_temporal_history({"events": [None] * 20001}) == "HISTORY_UNAVAILABLE"


def test_temporal_alert_includes_failure_codes_and_actions_without_secrets(monkeypatch):
    failed = [
        {"type": {"name": "SourceIngestWorkflow"},
         "execution": {"workflowId": "sensitive-123", "runId": "run-a"}},
        {"type": {"name": "SourceIngestWorkflow"},
         "execution": {"workflowId": "sensitive-456", "runId": "run-b"}},
        {"type": {"name": "DiscoveryWorkflow"},
         "execution": {"workflowId": "sensitive-789", "runId": "run-c"}},
        {"type": {"name": "DiscoveryWorkflow"},
         "execution": {"workflowId": "sensitive-999", "runId": "run-d"}},
    ]
    show_ids = []

    def command(*args, **kwargs):
        if "list" in args:
            if any('ExecutionStatus="Failed"' in str(arg) for arg in args):
                return json.dumps(failed)
            return "[]"
        if "show" in args:
            assert kwargs["timeout"] == 8
            show_ids.append(args[args.index("--workflow-id") + 1])
            return json.dumps({"events": [
                {"workflowExecutionFailedEventAttributes": {
                    "failure": {"message": "HTTP 429 SECRET_API_KEY=123"}}}
            ]})
        raise AssertionError("Unexpected command")

    monkeypatch.setattr(ops, "command", command)
    warning = ops.temporal_signal()
    assert warning.status == "warning"
    assert "4 failed executions" in warning.detail
    assert "SourceIngestWorkflow (2)" in warning.detail
    assert "RATE_LIMIT (3)" in warning.detail
    assert "3 sampled closed runs" in warning.detail
    assert "provider quota" in warning.action
    assert len(show_ids) == 3
    assert "sensitive-" not in warning.detail + warning.action
    assert "SECRET_API_KEY" not in warning.detail + warning.action


def test_temporal_history_probe_failure_preserves_failed_count(monkeypatch):
    failed = [{"type": {"name": "AcquisitionWorkflow"},
               "execution": {"workflowId": "secret-workflow", "runId": "run"}}]

    def command(*args, **kwargs):
        if "list" in args:
            if any('ExecutionStatus="Failed"' in str(arg) for arg in args):
                return json.dumps(failed)
            return "[]"
        if "show" in args:
            raise ops.ProbeError("temporal exceeded its diagnostic time limit.")
        raise AssertionError("Unexpected command")

    monkeypatch.setattr(ops, "command", command)
    warning = ops.temporal_signal()
    assert warning.status == "warning"
    assert "1 failed executions" in warning.detail
    assert "HISTORY_UNAVAILABLE" in warning.detail
    assert "Inspect the exact failed execution" in warning.action
    assert "secret-workflow" not in warning.detail + warning.action



def test_temporal_rolling_window_expiry_is_not_verified_recovery(ledger):
    failed = ops.Signal("temporal", "Background workflows", "warning", "1 failed", "Investigate")
    quiet = ops.Signal("temporal", "Background workflows", "healthy", "0 failed recently")

    ops.reconcile(ledger, [failed], NOW)
    for step in range(1, 73):
        ops.reconcile(ledger, [quiet], NOW + step * 300)
    incident = ledger.execute("SELECT * FROM incidents WHERE key='temporal'").fetchone()
    assert incident["resolved"] is None
    assert ledger.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    snapshot_signals = json.loads(
        ledger.execute("SELECT value FROM state WHERE key='signals'").fetchone()[0]
    )
    assert snapshot_signals[0]["status"] == "warning"
    assert "quiet period" in snapshot_signals[0]["detail"]

    ops.reconcile(ledger, [quiet], NOW + 73 * 300)
    incident = ledger.execute("SELECT * FROM incidents WHERE key='temporal'").fetchone()
    assert incident["resolved"] == NOW + 73 * 300
    assert incident["bot"] == "quieted"
    messages = [
        row[0] for row in ledger.execute("SELECT message FROM events ORDER BY id")
    ]
    assert len(messages) == 2
    assert "six hours without observed failures" in messages[-1]
    assert not any("recovery verified" in message for message in messages)


def test_temporal_new_failure_or_monitor_gap_restarts_quiet_clock(ledger):
    failed = ops.Signal("temporal", "Background workflows", "warning", "failed")
    quiet = ops.Signal("temporal", "Background workflows", "healthy", "no recent failures")
    ops.reconcile(ledger, [failed], NOW)
    ops.reconcile(ledger, [quiet], NOW + 300)
    ops.reconcile(ledger, [quiet], NOW + 7 * 3600)
    assert ledger.execute("SELECT resolved FROM incidents").fetchone()[0] is None

    ops.reconcile(ledger, [failed], NOW + 7 * 3600 + 300)
    assert ledger.execute(
        "SELECT 1 FROM state WHERE key='temporal_quiet_since'"
    ).fetchone() is None
    assert ledger.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    # A repeated observation while the incident stays active cannot page again.
    ops.reconcile(ledger, [failed], NOW + 7 * 3600 + 301)
    assert ledger.execute("SELECT count(*) FROM events").fetchone()[0] == 2

    ops.reconcile(ledger, [quiet], NOW + 7 * 3600 + 600)
    assert float(
        ledger.execute("SELECT value FROM state WHERE key='temporal_quiet_since'")
        .fetchone()[0]
    ) == NOW + 7 * 3600 + 600
    events = [row[0] for row in ledger.execute("SELECT message FROM events ORDER BY id")]
    assert "new failures detected after a quiet period" in events[-1]
    # A genuine new warning after another quiet observation produces a new event.
    ops.reconcile(ledger, [failed], NOW + 7 * 3600 + 900)
    assert ledger.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_drift_warns_before_review_threshold_even_if_ci_not_eligible(monkeypatch, tmp_path):
    from katcha.ops import production_release_observer as observer
    from katcha.ops import release_eligibility

    monkeypatch.setattr(observer, "read_pins", lambda path: {"KATCHA_RELEASE_SHA": "a" * 40})
    monkeypatch.setattr(observer, "current_main", lambda fn: "b" * 40)
    monkeypatch.setattr(
        observer,
        "compare_releases",
        lambda *args: {"commits": 60, "changed_files": 10, "migration_changes": True},
    )
    monkeypatch.setattr(
        release_eligibility, "check_release", lambda *args: pytest.fail("CI gates drift")
    )
    rows = ops.release_signals()
    assert rows[0].status == "warning"
    assert rows[1].status == "decision"
    assert "b" * 40 in rows[1].detail


def test_release_cache_limits_remote_checks(ledger, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ops,
        "release_signals",
        lambda: calls.append(1) or [ops.Signal("drift", "Cadence", "healthy", "Current")],
    )
    ops.cached_release_signals(ledger, NOW)
    ops.cached_release_signals(ledger, NOW + 300)
    ops.cached_release_signals(ledger, NOW + 900)
    assert len(calls) == 2


def test_completed_rollout_closes_release_review_incident(ledger, monkeypatch):
    from katcha.ops import production_release_observer as observer

    monkeypatch.setattr(observer, "read_pins", lambda path: {"KATCHA_RELEASE_SHA": "a" * 40})
    monkeypatch.setattr(observer, "current_main", lambda fn: "a" * 40)
    ops.reconcile(ledger, [ops.Signal("release", "Release review", "decision", "Candidate")], NOW)
    ops.reconcile(ledger, ops.release_signals(), NOW + 1)
    assert ledger.execute("SELECT resolved FROM incidents").fetchone()[0] == NOW + 1


def test_finished_safety_retry_without_proof_escalates_promptly(ledger, monkeypatch, tmp_path):
    calls = []
    ready(monkeypatch, tmp_path, calls)
    ops.reconcile(ledger, signals(), NOW)
    ops.remediate(ledger, NOW)
    ops.remediate(ledger, NOW + 300)
    assert ledger.execute("SELECT bot FROM incidents").fetchone()[0] == "operator_decision"
    assert len(calls) == 1


def test_command_errors_preserve_safe_codes_without_raw_output(monkeypatch):
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda *args, **kw: SimpleNamespace(returncode=7, stdout="SECRET", stderr="PRIVATE"),
    )
    with pytest.raises(ops.ProbeError, match="systemctl exited with status 7") as error:
        ops.command("systemctl", "show", "katcha-backup.service")
    assert "SECRET" not in str(error.value)
    assert "PRIVATE" not in str(error.value)


def test_release_bot_can_only_call_diagnostic_mode(ledger, monkeypatch):
    calls = []
    sha = "a" * 40
    monkeypatch.setattr(ops, "trusted_file", lambda path: True)

    def command(*args, **kwargs):
        calls.append(args)
        return json.dumps(
            {
                "target_sha": sha,
                "checks": [
                    {"name": "migration_review", "status": "REVIEW_REQUIRED", "detail": "SECRET"}
                ],
            }
        )

    monkeypatch.setattr(ops, "command", command)
    ops.reconcile(ledger, [ops.Signal("release", "Release", "decision", "Candidate " + sha)], NOW)
    ops.remediate(ledger, NOW)
    ops.remediate(ledger, NOW + 5)
    assert calls == [(str(ops.DEPLOYER), "--diagnose-release", sha)]
    assert "SECRET" not in str([tuple(row) for row in ledger.execute("SELECT * FROM events")])

    review = json.loads(ledger.execute("SELECT data FROM release_reviews").fetchone()[0])
    assert review["checks"] == [{"name": "migration_review", "status": "REVIEW_REQUIRED"}]
    assert "SECRET" not in json.dumps(review)
    events = ledger.execute("SELECT count(*) FROM events").fetchone()[0]
    ops.reconcile(
        ledger, [ops.Signal("release", "Release", "decision", "Candidate " + sha)], NOW + 3601
    )
    ops.remediate(ledger, NOW + 3601)
    assert len(calls) == 2
    assert ledger.execute("SELECT count(*) FROM events").fetchone()[0] == events


def test_snapshot_unavailable_stale_and_delivery_failure(ledger, monkeypatch, tmp_path):
    monkeypatch.setattr(view, "SNAPSHOT", tmp_path / "public/health.json")
    assert view.read_health(now=NOW)["status"] == "unavailable"
    ops.reconcile(ledger, signals("healthy"), NOW)
    ops.snapshot(ledger, tmp_path)
    assert view.read_health(now=NOW)["status"] == "healthy"
    assert view.read_health(now=NOW + 901)["status"] == "stale"
    ledger.execute("INSERT OR REPLACE INTO state VALUES ('delivery', 'failed')")
    ops.snapshot(ledger, tmp_path)
    assert view.read_health(now=NOW)["status"] == "attention"
    assert (tmp_path / "ledger.sqlite3").stat().st_mode & 0o077 == 0
    assert (tmp_path / "public").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    "scopes,channels,allowed",
    [({"*"}, None, True), ({"*"}, ["channel"], False), ({"ai:read", "channels:read"}, None, False)],
)
def test_global_operations_do_not_leak_across_channel_principals(
    monkeypatch, scopes, channels, allowed
):
    request = Request({"type": "http"})
    request.state.control_scopes = scopes
    request.state.control_channel_ids = channels
    monkeypatch.setattr("katcha.api.control_auth.control_allowed_channel_ids", lambda req: channels)
    monkeypatch.setattr(view, "read_health", lambda: {"status": "healthy"})
    if allowed:
        assert operations_health(request)["status"] == "healthy"
    else:
        with pytest.raises(HTTPException) as denied:
            operations_health(request)
        assert denied.value.status_code == 403


def test_public_heartbeat_contains_only_freshness(monkeypatch):
    from katcha.api.main import operations_heartbeat

    monkeypatch.setattr(
        view,
        "read_health",
        lambda: {"status": "attention", "checked_at": NOW, "incidents": ["PRIVATE"]},
    )
    response = Response()
    assert operations_heartbeat(response) == {"status": "reporting", "checked_at": NOW}
    assert response.status_code == 200
    monkeypatch.setattr(view, "read_health", lambda: {"status": "stale", "checked_at": NOW})
    assert operations_heartbeat(response)["status"] == "unavailable"
    assert response.status_code == 503


def test_alert_delivery_failure_reaches_external_watchdog(monkeypatch):
    from katcha.api.main import operations_heartbeat

    monkeypatch.setattr(
        view,
        "read_health",
        lambda: {
            "status": "attention",
            "checked_at": NOW,
            "notifications": {"status": "failed", "pending": 1},
        },
    )
    response = Response()
    assert operations_heartbeat(response)["status"] == "unavailable"
    assert response.status_code == 503


@pytest.mark.parametrize(
    "corruption",
    [
        {"notifications": None},
        {"signals": [None]},
        {"checked_at": float("nan")},
        {"history": "bad"},
    ],
)
def test_invalid_snapshots_are_unavailable(ledger, monkeypatch, tmp_path, corruption):
    ops.reconcile(ledger, signals("healthy"), NOW)
    data = ops.snapshot(ledger, tmp_path)
    data.update(corruption)
    target = tmp_path / "public/health.json"
    target.write_text(json.dumps(data))
    monkeypatch.setattr(view, "SNAPSHOT", target)
    assert view.read_health(now=NOW)["status"] == "unavailable"


def test_health_api_authentication_and_public_heartbeat(monkeypatch):
    from fastapi.testclient import TestClient

    import katcha.api.control_auth as auth
    from katcha.api.main import app
    from katcha.config import Settings

    token = "operator-fixture-token-0000001"
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: Settings(_env_file=None, control_api_token=token, control_api_scopes="*"),
    )
    monkeypatch.setattr(
        view,
        "read_health",
        lambda: {"status": "healthy", "checked_at": NOW, "incidents": ["PRIVATE"]},
    )
    client = TestClient(app)
    assert client.get("/v1/operations/health").status_code == 401
    result = client.get("/v1/operations/health", headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    assert result.json()["incidents"] == ["PRIVATE"]
    public = client.get("/v1/health/operations")
    assert public.status_code == 200
    assert "PRIVATE" not in public.text
