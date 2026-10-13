"""Expiry cleanup must not depend on the recovery launch enable switch.

Static workflow contract only; no OCI calls or GitHub workflow dispatches.
"""
import re
from pathlib import Path

WORKFLOW = Path(".github/workflows/oci-recovery.yml")


def _job_block(name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("  " + name + ":\n")
    remaining = text[start + 2:]
    # Find the next actual top-level job, not indented job attributes.
    next_job = re.search(r"(?m)^  [a-z][a-z0-9-]*:\s*$", remaining[len(name) + 2:])
    if next_job is None:
        return remaining
    return remaining[: len(name) + 2 + next_job.start()]


def test_scheduled_cleanup_independent_of_recovery_activation():
    cleanup = _job_block("paid-fallback-expiry")
    assert "github.event_name == 'schedule'" in cleanup
    assert "github.event.schedule == '7,22,37,52 * * * *'" in cleanup
    assert "vars.KATCHA_OCI_CLEANUP_APPROVED == 'true'" in cleanup
    assert "vars.KATCHA_OCI_RECOVERY_CONFIGURED" not in cleanup
    assert "python -m katcha.ops.oci_recovery cleanup" in cleanup
    assert "python -m katcha.ops.oci_recovery recover" not in cleanup
    assert "compute instance launch" not in cleanup.lower()


def test_production_recovery_launch_gates_stay_enabled():
    recover = _job_block("recover")
    assert "KATCHA_OCI_RECOVERY_CONFIGURED" in recover
    assert "KATCHA_EXTERNAL_COMPUTE_ENABLED" in recover
    assert "exit 41" in recover
    assert "exit 42" in recover
    watchdog = _job_block("recovery-watchdog-backstop")
    assert "vars.KATCHA_OCI_RECOVERY_CONFIGURED == 'true'" in watchdog


def test_cleanup_gate_is_not_set_true_in_workflow():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "KATCHA_OCI_CLEANUP_APPROVED: true" not in text
    assert "gh variable set KATCHA_OCI_CLEANUP_APPROVED" not in text
