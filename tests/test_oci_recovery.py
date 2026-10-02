from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from katcha.ops import oci_recovery


ROOT = Path(__file__).resolve().parents[1]


def _base_env(monkeypatch, tmp_path: Path) -> None:
    template = ROOT / "deploy" / "cloud-init" / "oci-recovery-candidate.sh.tmpl"
    values = {
        "KATCHA_EXTERNAL_COMPUTE_ENABLED": "true",
        "KATCHA_RECOVERY_COORDINATOR_URL": "https://recovery.katcha.test",
        "KATCHA_RECOVERY_ADMIN_TOKEN": "admin-token",
        "KATCHA_RECOVERY_CANDIDATE_TOKEN": "candidate-token",
        "KATCHA_PUBLIC_HEALTH_URL": "https://katcha.test/v1/health/ready",
        "KATCHA_RELEASE_SHA": "a" * 40,
        "KATCHA_OCI_AVAILABILITY_DOMAIN": "TEST:AD-1",
        "KATCHA_OCI_COMPARTMENT_ID": "ocid1.compartment.test",
        "KATCHA_OCI_SUBNET_ID": "ocid1.subnet.test",
        "KATCHA_OCI_DATA_VOLUME_ID": "ocid1.volume.test",
        "KATCHA_OCI_DATA_VOLUME_FS_UUID": "deadbeef-dead-beef-dead-beefdeadbeef",
        "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID": "ocid1.vaultsecret.env",
        "KATCHA_OCI_AWS_BUNDLE_SECRET_ID": "ocid1.vaultsecret.aws",
        "KATCHA_OCI_PRIMARY_IMAGE_ID": "ocid1.image.arm",
        "KATCHA_OCI_PAID_FALLBACK_SHAPE": "VM.Standard.E5.Flex",
        "KATCHA_OCI_PAID_FALLBACK_IMAGE_ID": "ocid1.image.amd",
        "KATCHA_OCI_PAID_FALLBACK_ENABLED": "true",
        "KATCHA_OCI_PAID_FALLBACK_TTL_HOURS": "12",
        "KATCHA_OCI_PAID_FALLBACK_ESTIMATED_HOURLY_USD": "0.10",
        "KATCHA_OCI_PAID_FALLBACK_MAX_INCIDENT_USD": "1.20",
        "KATCHA_OCI_PAID_FALLBACK_MAX_CONCURRENT": "1",
        "KATCHA_OCI_RECOVERY_BOOTSTRAP_TEMPLATE": str(template),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_paid_budget_rejects_ttl_that_can_exceed_cap(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_OCI_PAID_FALLBACK_TTL_HOURS", "24")
    monkeypatch.setenv("KATCHA_OCI_PAID_FALLBACK_MAX_INCIDENT_USD", "1.00")

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="TTL exceeds the configured incident budget",
    ):
        oci_recovery.RecoveryConfig.from_env()


def test_external_compute_kill_switch_fails_closed(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_EXTERNAL_COMPUTE_ENABLED", "false")

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="external compute kill switch is disabled",
    ):
        oci_recovery.RecoveryConfig.from_env()


def test_candidate_lookup_and_paid_count_ignore_terminated_instances() -> None:
    rows = [
        {
            "id": "primary",
            "lifecycle-state": "RUNNING",
            "freeform-tags": {
                "KatchaRecoveryIncident": "incident-1",
                "KatchaRecoveryMode": "always-free-a1",
            },
        },
        {
            "id": "old-paid",
            "lifecycle-state": "TERMINATED",
            "freeform-tags": {"KatchaRecoveryMode": "paid-fallback"},
        },
        {
            "id": "paid",
            "lifecycle-state": "STOPPED",
            "freeform-tags": {"KatchaRecoveryMode": "paid-fallback"},
        },
    ]

    assert (
        oci_recovery.find_existing_candidate(rows, "incident-1")["id"]
        == "primary"
    )
    assert oci_recovery.paid_instance_count(rows) == 1


def test_cleanup_terminates_only_expired_paid_instances(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    now = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)

    class FakeOci:
        def __init__(self) -> None:
            self.softstopped = []
            self.terminated = []

        def list_instances(self, _config):
            return [
                {
                    "id": "expired",
                    "lifecycle-state": "RUNNING",
                    "freeform-tags": {
                        "KatchaRecoveryMode": "paid-fallback",
                        "KatchaExpiresAt": (now - timedelta(minutes=1)).isoformat(),
                    },
                },
                {
                    "id": "future",
                    "lifecycle-state": "RUNNING",
                    "freeform-tags": {
                        "KatchaRecoveryMode": "paid-fallback",
                        "KatchaExpiresAt": (now + timedelta(hours=1)).isoformat(),
                    },
                },
                {
                    "id": "free",
                    "lifecycle-state": "RUNNING",
                    "freeform-tags": {
                        "KatchaRecoveryMode": "always-free-a1",
                    },
                },
            ]

        def softstop(self, instance_id):
            self.softstopped.append(instance_id)

        def terminate(self, instance_id):
            self.terminated.append(instance_id)

    oci = FakeOci()
    terminated = oci_recovery.cleanup_expired_paid(
        config,
        oci,
        now=now,
    )

    assert terminated == ["expired"]
    assert oci.softstopped == ["expired"]
    assert oci.terminated == ["expired"]


def test_recovery_uses_paid_only_after_a1_capacity_failure(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "client_payload": {
                    "incident_id": "12345678-1234-1234-1234-123456789012",
                    "expected_active_epoch": 7,
                    "active_deployment": {
                        "deployment_id": "oci-a1-old",
                        "epoch": 7,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeCoordinator:
        instance = None

        def __init__(self, _config):
            self.committed = False
            self.aborted = False
            FakeCoordinator.instance = self

        def status(self):
            return {
                "active": {"deployment_id": "oci-a1-old", "epoch": 7},
                "incident": {"id": "12345678-1234-1234-1234-123456789012"},
            }

        def prepare(self, **_kwargs):
            return {
                "pending": {
                    "deployment_id": "oci-recovery-12345678-123",
                    "epoch": 8,
                }
            }

        def commit(self, **_kwargs):
            self.committed = True
            return {"leader_id": "oci-recovery-12345678-123"}

        def abort(self, **_kwargs):
            self.aborted = True

    class FakeOci:
        def __init__(self):
            self.launch_modes = []

        def list_instances(self, _config):
            return []

        def launch(self, _config, _incident, *, plan, **_kwargs):
            self.launch_modes.append(plan.mode)
            if not plan.paid:
                raise oci_recovery.CapacityUnavailable("OutOfHostCapacity")
            return "ocid1.instance.paid"

        def volume_attachments(self, _config):
            return []

        def attach(self, _config, _instance_id):
            return None

        def terminate(self, _instance_id):
            raise AssertionError("successful candidate must not be terminated")

    oci = FakeOci()
    monkeypatch.setattr(oci_recovery, "CoordinatorClient", FakeCoordinator)
    monkeypatch.setattr(
        oci_recovery,
        "wait_for_candidate_ready",
        lambda *_args, **_kwargs: None,
    )

    result = oci_recovery.recover(event, config, oci)

    assert oci.launch_modes == ["always-free-a1", "paid-fallback"]
    assert result["mode"] == "paid-fallback"
    assert result["status"] == "committed"
    assert FakeCoordinator.instance.committed is True
    assert FakeCoordinator.instance.aborted is False


def test_stale_incident_is_rejected_before_compute() -> None:
    with pytest.raises(oci_recovery.RecoveryError, match="active epoch is stale"):
        oci_recovery.validate_incident(
            {
                "active": {"deployment_id": "new", "epoch": 9},
                "incident": {"id": "incident-1"},
            },
            oci_recovery.Incident(
                incident_id="incident-1",
                expected_active_epoch=8,
                active_deployment_id="old",
            ),
        )


def test_real_bootstrap_template_renders_without_touching_shell_syntax(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "KATCHA_RECOVERY_CANDIDATE_TOKEN",
        "candidate-token-with-'quote",
    )
    config = oci_recovery.RecoveryConfig.from_env()

    rendered = oci_recovery.render_bootstrap(
        config,
        deployment_id="oci-recovery-test",
        deployment_epoch=9,
    )
    try:
        content = rendered.read_text(encoding="utf-8")
        assert "__DEPLOYMENT_ID__" not in content
        assert "oci-recovery-test" in content
        assert 'printf \'[katcha-recovery] %s\\n\' "$1"' in content
        subprocess.run(["bash", "-n", str(rendered)], check=True)
    finally:
        rendered.unlink(missing_ok=True)


def test_free_recovery_does_not_require_paid_shape_or_image(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_OCI_PAID_FALLBACK_ENABLED", "false")
    monkeypatch.delenv("KATCHA_OCI_PAID_FALLBACK_SHAPE", raising=False)
    monkeypatch.delenv("KATCHA_OCI_PAID_FALLBACK_IMAGE_ID", raising=False)

    config = oci_recovery.RecoveryConfig.from_env()

    assert config.paid_enabled is False
    assert config.fallback.shape == "disabled"
    assert config.fallback.image_id == config.primary.image_id


def test_commit_reconciles_success_when_response_is_lost() -> None:
    class Coordinator:
        def commit(self, **_kwargs):
            raise OSError("response lost after commit")

        def status(self):
            return {
                "active": {
                    "deployment_id": "candidate",
                    "epoch": 8,
                },
                "pending": None,
            }

    result = oci_recovery.commit_authority_safely(
        Coordinator(),
        deployment_id="candidate",
        deployment_epoch=8,
        expected_active_epoch=7,
    )

    assert result["leader_id"] == "candidate"
    assert result["reconciled_after_commit_error"] is True


def test_commit_unknown_never_claims_safe_rollback() -> None:
    class Coordinator:
        def commit(self, **_kwargs):
            raise OSError("response lost")

        def status(self):
            raise OSError("coordinator unreachable")

    with pytest.raises(
        oci_recovery.CommitOutcomeUnknown,
        match="must not be rolled back automatically",
    ):
        oci_recovery.commit_authority_safely(
            Coordinator(),
            deployment_id="candidate",
            deployment_epoch=8,
            expected_active_epoch=7,
        )


def test_confirmed_pending_commit_failure_is_rollback_safe() -> None:
    class Coordinator:
        def commit(self, **_kwargs):
            raise OSError("commit rejected")

        def status(self):
            return {
                "active": {
                    "deployment_id": "old",
                    "epoch": 7,
                },
                "pending": {
                    "deployment_id": "candidate",
                    "epoch": 8,
                },
            }

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="still pending",
    ):
        oci_recovery.commit_authority_safely(
            Coordinator(),
            deployment_id="candidate",
            deployment_epoch=8,
            expected_active_epoch=7,
        )
