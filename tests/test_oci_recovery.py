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
        "KATCHA_EXTERNAL_COMPUTE_TOKEN": "external-compute-token",
        "KATCHA_PUBLIC_HEALTH_URL": "https://katcha.test/v1/health/ready",
        "KATCHA_RELEASE_SHA": "a" * 40,
        "KATCHA_OCI_AVAILABILITY_DOMAIN": "TEST:AD-1",
        "KATCHA_OCI_COMPARTMENT_ID": "ocid1.compartment.test",
        "KATCHA_OCI_SUBNET_ID": "ocid1.subnet.ad1",
        "KATCHA_OCI_CROSS_AD_TARGETS_JSON": json.dumps(
            [
                {
                    "availability_domain": "TEST:AD-2",
                    "subnet_id": "ocid1.subnet.ad2",
                }
            ]
        ),
        "KATCHA_OCI_CROSS_AD_DATA_VOLUME_SIZE_GB": "50",
        "KATCHA_OCI_DATA_VOLUME_DEVICE_PATH": "/dev/oracleoci/oraclevdb",
        "KATCHA_OCI_CROSS_AD_MAX_BACKUP_AGE_SECONDS": "7200",
        "KATCHA_OCI_DATA_VOLUME_ID": "ocid1.volume.test",
        "KATCHA_OCI_DATA_VOLUME_FS_UUID": "deadbeef-dead-beef-dead-beefdeadbeef",
        "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID": "ocid1.vaultsecret.env",
        "KATCHA_OCI_AWS_BUNDLE_SECRET_ID": "ocid1.vaultsecret.aws",
        "KATCHA_OCI_BACKUP_ENV_SECRET_ID": "ocid1.vaultsecret.backup",
        "KATCHA_OCI_RESTORE_ENV_SECRET_ID": "ocid1.vaultsecret.restore",
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


def _event(tmp_path: Path) -> Path:
    path = tmp_path / "event.json"
    path.write_text(
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
    return path


class FakeCoordinator:
    instance = None

    def __init__(self, _config):
        self.committed = False
        self.aborted = False
        self.reserved: list[dict[str, object]] = []
        self.settled: list[tuple[str, int]] = []
        self.released: list[str] = []
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

    def reserve_external_compute(self, **kwargs):
        self.reserved.append(dict(kwargs))
        return {
            "reservation": {
                "id": f"reservation-{len(self.reserved)}",
                "status": "reserved",
                "estimated_cost_microusd": kwargs["estimated_cost_microusd"],
            },
            "reused": False,
        }

    def settle_external_compute(
        self,
        reservation_id,
        *,
        actual_cost_microusd,
        metadata=None,
    ):
        del metadata
        self.settled.append((reservation_id, actual_cost_microusd))
        return {
            "reservation": {
                "id": reservation_id,
                "status": "settled",
                "actual_cost_microusd": actual_cost_microusd,
            },
            "reused": False,
        }

    def release_external_compute(self, reservation_id, *, reason):
        del reason
        self.released.append(reservation_id)
        return {
            "reservation": {
                "id": reservation_id,
                "status": "released",
            },
            "reused": False,
        }


class RecoveryOci:
    def __init__(self) -> None:
        self.terminated: list[str] = []

    def list_instances(self, _config):
        return [
            {
                "id": "ocid1.instance.old",
                "lifecycle-state": "RUNNING",
                "availability-domain": "TEST:AD-1",
                "freeform-tags": {
                    "KatchaDeploymentId": "oci-a1-old",
                    "KatchaDataVolumeId": "ocid1.volume.test",
                    "KatchaSubnetId": "ocid1.subnet.ad1",
                },
            }
        ]

    def get_instance(self, instance_id):
        for row in self.list_instances(None):
            if row["id"] == instance_id:
                return row
        return {"id": instance_id, "lifecycle-state": "RUNNING", "freeform-tags": {}}

    def terminate(self, instance_id):
        self.terminated.append(instance_id)


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


def test_oci_vault_recovery_requires_secret_ocids(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID",
        "not-a-secret-ocid",
    )

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="KATCHA_OCI_PRODUCTION_ENV_SECRET_ID must be an OCI Vault secret OCID",
    ):
        oci_recovery.RecoveryConfig.from_env()


def test_cross_ad_targets_require_unique_ad_subnet_pairs(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "KATCHA_OCI_CROSS_AD_TARGETS_JSON",
        json.dumps(
            [
                {
                    "availability_domain": "TEST:AD-1",
                    "subnet_id": "ocid1.subnet.ad1",
                }
            ]
        ),
    )

    with pytest.raises(oci_recovery.RecoveryError, match="must be unique"):
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
            "freeform-tags": {"KatchaPaidFallback": "true"},
        },
        {
            "id": "paid",
            "lifecycle-state": "STOPPED",
            "freeform-tags": {
                "KatchaRecoveryMode": "cross-ad-paid-fallback",
                "KatchaPaidFallback": "true",
            },
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
                        "KatchaRecoveryMode": "cross-ad-paid-fallback",
                        "KatchaPaidFallback": "true",
                        "KatchaExpiresAt": (now - timedelta(minutes=1)).isoformat(),
                    },
                },
                {
                    "id": "future",
                    "lifecycle-state": "RUNNING",
                    "freeform-tags": {
                        "KatchaRecoveryMode": "paid-fallback",
                        "KatchaPaidFallback": "true",
                        "KatchaExpiresAt": (now + timedelta(hours=1)).isoformat(),
                    },
                },
                {
                    "id": "free",
                    "lifecycle-state": "RUNNING",
                    "freeform-tags": {
                        "KatchaRecoveryMode": "cross-ad-free-a1",
                    },
                },
            ]

        def softstop(self, instance_id):
            self.softstopped.append(instance_id)

        def terminate(self, instance_id):
            self.terminated.append(instance_id)

    class Coordinator:
        def __init__(self):
            self.settled = []

        def settle_external_compute(
            self,
            reservation_id,
            *,
            actual_cost_microusd,
            metadata=None,
        ):
            del metadata
            self.settled.append((reservation_id, actual_cost_microusd))
            return {"reservation": {"status": "settled"}}

    oci = FakeOci()
    original_list = oci.list_instances

    def list_instances_with_budget(_config):
        rows = original_list(_config)
        for row in rows:
            if row["id"] == "expired":
                row["freeform-tags"].update(
                    {
                        "KatchaBudgetReservationId": "reservation-expired",
                        "KatchaBudgetReservedMicrousd": "1200000",
                        "KatchaPaidStartedAt": (
                            now - timedelta(hours=2)
                        ).isoformat(),
                        "KatchaEstimatedHourlyUsd": "0.10",
                    }
                )
        return rows
    oci.list_instances = list_instances_with_budget
    coordinator = Coordinator()
    terminated = oci_recovery.cleanup_expired_paid(
        config,
        oci,
        coordinator,
        now=now,
    )

    assert terminated == ["expired"]
    assert oci.softstopped == ["expired"]
    assert oci.terminated == ["expired"]
    assert coordinator.settled == [("reservation-expired", 198_334)]


def test_recovery_prefers_cross_ad_free_before_paid(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    oci = RecoveryOci()
    attempted: list[tuple[str, str, str]] = []

    def fake_launch_attempt(**kwargs):
        attempted.append(
            (
                kwargs["recovery_mode"],
                kwargs["target"].availability_domain,
                kwargs["storage_mode"],
            )
        )
        if kwargs["recovery_mode"] == "always-free-a1":
            raise oci_recovery.CapacityUnavailable("OutOfHostCapacity")
        if kwargs["recovery_mode"] == "cross-ad-free-a1":
            return oci_recovery.CandidateAttempt(
                instance_id="ocid1.instance.crossad",
                target=kwargs["target"],
                volume_id="ocid1.volume.crossad",
                storage_mode="r2-restore",
                recovery_mode="cross-ad-free-a1",
                previous_instance_id=None,
                owns_recovery_volume=True,
            )
        raise AssertionError("paid fallback must not run when alternate free A1 works")

    monkeypatch.setattr(oci_recovery, "CoordinatorClient", FakeCoordinator)
    monkeypatch.setattr(oci_recovery, "_launch_attempt", fake_launch_attempt)

    result = oci_recovery.recover(_event(tmp_path), config, oci)

    assert attempted == [
        ("always-free-a1", "TEST:AD-1", "existing-volume"),
        ("cross-ad-free-a1", "TEST:AD-2", "r2-restore"),
    ]
    assert result["mode"] == "cross-ad-free-a1"
    assert result["storage_mode"] == "r2-restore"
    assert result["availability_domain"] == "TEST:AD-2"
    assert result["data_volume_id"] == "ocid1.volume.crossad"
    assert oci.terminated == ["ocid1.instance.old"]
    assert FakeCoordinator.instance.committed is True
    assert FakeCoordinator.instance.aborted is False


def test_recovery_uses_paid_only_after_all_free_targets_fail(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    oci = RecoveryOci()
    attempted: list[str] = []

    def fake_launch_attempt(**kwargs):
        mode = kwargs["recovery_mode"]
        attempted.append(mode)
        if mode in {"always-free-a1", "cross-ad-free-a1"}:
            raise oci_recovery.CapacityUnavailable("OutOfHostCapacity")
        if mode == "paid-fallback":
            return oci_recovery.CandidateAttempt(
                instance_id="ocid1.instance.paid",
                target=kwargs["target"],
                volume_id="ocid1.volume.test",
                storage_mode="existing-volume",
                recovery_mode=mode,
                previous_instance_id="ocid1.instance.old",
                owns_recovery_volume=False,
                budget_reservation_id=kwargs["budget_reservation_id"],
                budget_reserved_microusd=kwargs["budget_reserved_microusd"],
            )
        raise AssertionError("cross-AD paid fallback should not be needed")

    monkeypatch.setattr(oci_recovery, "CoordinatorClient", FakeCoordinator)
    monkeypatch.setattr(oci_recovery, "_launch_attempt", fake_launch_attempt)

    result = oci_recovery.recover(_event(tmp_path), config, oci)

    assert attempted == [
        "always-free-a1",
        "cross-ad-free-a1",
        "paid-fallback",
    ]
    assert result["mode"] == "paid-fallback"
    assert len(FakeCoordinator.instance.reserved) == 1
    assert FakeCoordinator.instance.reserved[0]["provider"] == "oci"
    assert FakeCoordinator.instance.reserved[0]["estimated_cost_microusd"] == 1_200_000


def test_active_cross_ad_instance_becomes_new_same_ad_recovery_home(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    row = {
        "availability-domain": "TEST:AD-2",
        "freeform-tags": {
            "KatchaSubnetId": "ocid1.subnet.ad2",
            "KatchaDataVolumeId": "ocid1.volume.ad2",
        },
    }

    target = oci_recovery._target_for_active(config, row)

    assert target == oci_recovery.RecoveryTarget(
        availability_domain="TEST:AD-2",
        subnet_id="ocid1.subnet.ad2",
    )
    assert oci_recovery._current_data_volume_id(config, row) == "ocid1.volume.ad2"
    assert [
        target.availability_domain
        for target in oci_recovery._cross_ad_targets(config, target)
    ] == ["TEST:AD-1"]


def test_failed_cross_ad_launch_deletes_fresh_volume(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()

    class FakeOci:
        def __init__(self):
            self.deleted = []

        def create_recovery_volume(self, *_args, **_kwargs):
            return "ocid1.volume.fresh"

        def launch(self, *_args, **_kwargs):
            raise oci_recovery.CapacityUnavailable("OutOfHostCapacity")

        def delete_volume(self, volume_id):
            self.deleted.append(volume_id)

    class Coordinator:
        pass

    oci = FakeOci()
    with pytest.raises(oci_recovery.CapacityUnavailable):
        oci_recovery._launch_attempt(
            config=config,
            oci=oci,
            coordinator=Coordinator(),
            incident=oci_recovery.Incident("incident", 7, "old"),
            deployment_id="candidate",
            deployment_epoch=8,
            plan=config.primary,
            target=config.alternate_targets[0],
            storage_mode="r2-restore",
            current_volume_id=config.data_volume_id,
            recovery_mode="cross-ad-free-a1",
        )

    assert oci.deleted == ["ocid1.volume.fresh"]


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
        storage_mode="r2-restore",
    )
    try:
        content = rendered.read_text(encoding="utf-8")
        assert "__DEPLOYMENT_ID__" not in content
        assert "oci-recovery-test" in content
        assert "ocid1.vaultsecret.backup" in content
        assert "ocid1.vaultsecret.restore" in content
        assert "/dev/oracleoci/oraclevdb" in content
        assert "r2-restore" in content
        assert "postgres-disaster-restore.sh" in content
        assert "install-production-units.sh --start" in content
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



def test_switch_volume_restores_previous_attachment_when_candidate_attach_fails(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()

    class FakeOci:
        def __init__(self) -> None:
            self.stopped: list[str] = []
            self.detached: list[str] = []
            self.attached: list[tuple[str, str]] = []
            self.started: list[str] = []

        def volume_attachments(self, _config, _volume_id):
            return [
                {
                    "id": "attachment-old",
                    "lifecycle-state": "ATTACHED",
                    "instance-id": "instance-old",
                }
            ]

        def stop(self, instance_id):
            self.stopped.append(instance_id)

        def detach(self, attachment_id):
            self.detached.append(attachment_id)

        def attach(self, instance_id, volume_id, *, device_path):
            del device_path
            self.attached.append((instance_id, volume_id))
            if instance_id == "instance-new":
                raise oci_recovery.RecoveryError("candidate attach failed")

        def start(self, instance_id):
            self.started.append(instance_id)

    oci = FakeOci()
    with pytest.raises(oci_recovery.RecoveryError, match="candidate attach failed"):
        oci_recovery.switch_volume(
            oci,
            config,
            "instance-new",
            "volume-1",
        )

    assert oci.stopped == ["instance-old"]
    assert oci.detached == ["attachment-old"]
    assert oci.attached == [
        ("instance-new", "volume-1"),
        ("instance-old", "volume-1"),
    ]
    assert oci.started == ["instance-old"]


def test_cleanup_attempt_continues_after_storage_cleanup_failure(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()

    class FakeOci:
        def __init__(self) -> None:
            self.terminated: list[str] = []
            self.deleted: list[str] = []

        def volume_attachments(self, _config, _volume_id):
            raise oci_recovery.RecoveryError("attachment lookup failed")

        def terminate(self, instance_id):
            self.terminated.append(instance_id)

        def delete_volume(self, volume_id):
            self.deleted.append(volume_id)

    oci = FakeOci()
    attempt = oci_recovery.CandidateAttempt(
        instance_id="instance-cross-ad",
        target=config.alternate_targets[0],
        volume_id="volume-cross-ad",
        storage_mode="r2-restore",
        recovery_mode="cross-ad-free-a1",
        previous_instance_id=None,
        owns_recovery_volume=True,
    )

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="cleanup incomplete",
    ):
        oci_recovery._cleanup_failed_attempt(oci, config, attempt)

    assert oci.terminated == ["instance-cross-ad"]
    assert oci.deleted == ["volume-cross-ad"]



def test_recovery_stops_after_incomplete_cleanup(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    oci = RecoveryOci()
    attempted: list[str] = []

    def fake_launch_attempt(**kwargs):
        attempted.append(kwargs["recovery_mode"])
        raise oci_recovery.RecoveryCleanupIncomplete(
            "candidate cleanup could not restore storage"
        )

    monkeypatch.setattr(oci_recovery, "CoordinatorClient", FakeCoordinator)
    monkeypatch.setattr(oci_recovery, "_launch_attempt", fake_launch_attempt)

    with pytest.raises(
        oci_recovery.RecoveryCleanupIncomplete,
        match="could not restore storage",
    ):
        oci_recovery.recover(_event(tmp_path), config, oci)

    assert attempted == ["always-free-a1"]
    assert FakeCoordinator.instance.aborted is True



def test_retired_volume_grace_period_is_bounded(monkeypatch, tmp_path) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_OCI_RETIRED_VOLUME_GRACE_HOURS", "12")

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="grace period must be between 24 and 720 hours",
    ):
        oci_recovery.RecoveryConfig.from_env()


def test_schedule_volume_retirement_preserves_existing_tags(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    now = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)

    class FakeOci:
        def __init__(self) -> None:
            self.updated = None

        def get_volume(self, volume_id):
            assert volume_id == "volume-old"
            return {
                "id": volume_id,
                "freeform-tags": {
                    "Owner": "katcha",
                    "DoNotLose": "true",
                },
            }

        def update_volume_tags(self, volume_id, tags):
            self.updated = (volume_id, tags)

    oci = FakeOci()
    retire_at = oci_recovery.schedule_volume_retirement(
        oci,
        config,
        volume_id="volume-old",
        replacement_volume_id="volume-new",
        replacement_deployment_id="deployment-new",
        now=now,
    )

    assert retire_at == (now + timedelta(hours=72)).isoformat()
    volume_id, tags = oci.updated
    assert volume_id == "volume-old"
    assert tags["Owner"] == "katcha"
    assert tags["DoNotLose"] == "true"
    assert tags["KatchaRetirementAuthorized"] == "true"
    assert tags["KatchaReplacedByVolume"] == "volume-new"
    assert tags["KatchaReplacedByDeployment"] == "deployment-new"


def test_cleanup_retired_volumes_deletes_only_expired_unattached_non_active(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()

    class Coordinator:
        def status(self):
            return {
                "active": {
                    "deployment_id": "leader",
                    "epoch": 9,
                }
            }

    class FakeOci:
        def __init__(self) -> None:
            self.deleted = []

        def list_instances(self, _config):
            return [
                {
                    "id": "leader-instance",
                    "availability-domain": "TEST:AD-2",
                    "lifecycle-state": "RUNNING",
                    "freeform-tags": {
                        "KatchaDeploymentId": "leader",
                        "KatchaDataVolumeId": "volume-active",
                    },
                }
            ]

        def list_volumes(self, _config):
            return [
                {
                    "id": "volume-active",
                    "lifecycle-state": "AVAILABLE",
                    "freeform-tags": {
                        "KatchaRetirementAuthorized": "true",
                        "KatchaRetireAfter": past,
                    },
                },
                {
                    "id": "volume-expired",
                    "lifecycle-state": "AVAILABLE",
                    "freeform-tags": {
                        "KatchaRetirementAuthorized": "true",
                        "KatchaRetireAfter": past,
                    },
                },
                {
                    "id": "volume-future",
                    "lifecycle-state": "AVAILABLE",
                    "freeform-tags": {
                        "KatchaRetirementAuthorized": "true",
                        "KatchaRetireAfter": future,
                    },
                },
                {
                    "id": "volume-attached",
                    "lifecycle-state": "AVAILABLE",
                    "freeform-tags": {
                        "KatchaRetirementAuthorized": "true",
                        "KatchaRetireAfter": past,
                    },
                },
                {
                    "id": "volume-unmanaged",
                    "lifecycle-state": "AVAILABLE",
                    "freeform-tags": {
                        "KatchaRetireAfter": past,
                    },
                },
            ]

        def volume_attachments(self, _config, volume_id):
            if volume_id == "volume-attached":
                return [
                    {
                        "instance-id": "some-instance",
                        "lifecycle-state": "ATTACHED",
                    }
                ]
            return []

        def delete_volume(self, volume_id):
            self.deleted.append(volume_id)

    oci = FakeOci()
    deleted = oci_recovery.cleanup_retired_volumes(
        config,
        oci,
        Coordinator(),
        now=now,
    )

    assert deleted == ["volume-expired"]
    assert oci.deleted == ["volume-expired"]


def test_cleanup_retired_volumes_fails_closed_on_coordinator_ambiguity(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()

    class Coordinator:
        def status(self):
            return {"active": {}}

    class FakeOci:
        def __init__(self) -> None:
            self.deleted = []

        def delete_volume(self, volume_id):
            self.deleted.append(volume_id)

    oci = FakeOci()
    with pytest.raises(
        oci_recovery.RecoveryError,
        match="no active deployment",
    ):
        oci_recovery.cleanup_retired_volumes(
            config,
            oci,
            Coordinator(),
        )

    assert oci.deleted == []



def test_break_glass_mode_does_not_require_oci_vault_secret_ids(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_RECOVERY_SECRET_SOURCE", "break-glass")
    monkeypatch.setenv(
        "KATCHA_BREAK_GLASS_HANDOFF_URL",
        "https://handoff.example.invalid/object?signature=fixture",
    )
    monkeypatch.setenv(
        "KATCHA_BREAK_GLASS_HANDOFF_KEY",
        "roUtmwTlzNzQht-sboiCMEq1azSRnyNjbzbET3CTKPk=",
    )
    for name in (
        "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID",
        "KATCHA_OCI_AWS_BUNDLE_SECRET_ID",
        "KATCHA_OCI_BACKUP_ENV_SECRET_ID",
        "KATCHA_OCI_RESTORE_ENV_SECRET_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    config = oci_recovery.RecoveryConfig.from_env()

    assert config.secret_source == "break-glass"
    assert config.production_env_secret_id == ""
    assert config.aws_bundle_secret_id == ""
    assert config.backup_env_secret_id == ""
    assert config.restore_env_secret_id == ""


def test_break_glass_mode_rejects_invalid_handoff_key(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_RECOVERY_SECRET_SOURCE", "break-glass")
    monkeypatch.setenv(
        "KATCHA_BREAK_GLASS_HANDOFF_URL",
        "https://handoff.example.invalid/object",
    )
    monkeypatch.setenv(
        "KATCHA_BREAK_GLASS_HANDOFF_KEY",
        "not-a-fernet-key",
    )

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="valid Fernet key",
    ):
        oci_recovery.RecoveryConfig.from_env()


def test_oci_vault_mode_still_requires_vault_secret_ids(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_RECOVERY_SECRET_SOURCE", "oci-vault")
    monkeypatch.delenv("KATCHA_OCI_PRODUCTION_ENV_SECRET_ID", raising=False)

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="KATCHA_OCI_PRODUCTION_ENV_SECRET_ID",
    ):
        oci_recovery.RecoveryConfig.from_env()


def test_break_glass_bootstrap_renders_without_vault_dependency(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KATCHA_RECOVERY_SECRET_SOURCE", "break-glass")
    monkeypatch.setenv(
        "KATCHA_BREAK_GLASS_HANDOFF_URL",
        "https://handoff.example.invalid/object?signature=fixture",
    )
    monkeypatch.setenv(
        "KATCHA_BREAK_GLASS_HANDOFF_KEY",
        "roUtmwTlzNzQht-sboiCMEq1azSRnyNjbzbET3CTKPk=",
    )
    for name in (
        "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID",
        "KATCHA_OCI_AWS_BUNDLE_SECRET_ID",
        "KATCHA_OCI_BACKUP_ENV_SECRET_ID",
        "KATCHA_OCI_RESTORE_ENV_SECRET_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    config = oci_recovery.RecoveryConfig.from_env()
    rendered = oci_recovery.render_bootstrap(
        config,
        deployment_id="oci-break-glass-test",
        deployment_epoch=10,
        storage_mode="r2-restore",
    )
    try:
        content = rendered.read_text(encoding="utf-8")
        assert "SECRET_SOURCE=break-glass" in content
        assert "install-handoff" in content
        assert "Restoring runtime secrets from OCI Vault" in content
        assert "case \"$SECRET_SOURCE\"" in content
        subprocess.run(["bash", "-n", str(rendered)], check=True)
    finally:
        rendered.unlink(missing_ok=True)



def test_paid_oci_launch_refuses_missing_budget_reservation(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    oci = oci_recovery.OciCli()
    monkeypatch.setattr(
        oci,
        "run",
        lambda _args: pytest.fail(
            "OCI transport must not run without a budget reservation"
        ),
    )
    user_data = tmp_path / "cloud-init.sh"
    user_data.write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(
        oci_recovery.RecoveryError,
        match="requires an external-compute budget reservation",
    ):
        oci.launch(
            config,
            oci_recovery.Incident("incident-1", 7, "old"),
            deployment_id="candidate",
            deployment_epoch=8,
            plan=config.fallback,
            target=oci_recovery.RecoveryTarget(
                config.availability_domain,
                config.subnet_id,
            ),
            recovery_mode="paid-fallback",
            data_volume_id=config.data_volume_id,
            storage_mode="existing-volume",
            user_data_path=user_data,
        )


def test_paid_capacity_failure_releases_budget_before_next_target(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    oci = RecoveryOci()
    attempted: list[str] = []

    def fake_launch_attempt(**kwargs):
        mode = kwargs["recovery_mode"]
        attempted.append(mode)
        if mode in {"always-free-a1", "cross-ad-free-a1", "paid-fallback"}:
            raise oci_recovery.CapacityUnavailable("OutOfHostCapacity")
        return oci_recovery.CandidateAttempt(
            instance_id="ocid1.instance.crossad-paid",
            target=kwargs["target"],
            volume_id="ocid1.volume.crossad-paid",
            storage_mode="r2-restore",
            recovery_mode=mode,
            previous_instance_id=None,
            owns_recovery_volume=True,
            budget_reservation_id=kwargs["budget_reservation_id"],
            budget_reserved_microusd=kwargs["budget_reserved_microusd"],
        )

    monkeypatch.setattr(oci_recovery, "CoordinatorClient", FakeCoordinator)
    monkeypatch.setattr(oci_recovery, "_launch_attempt", fake_launch_attempt)

    result = oci_recovery.recover(_event(tmp_path), config, oci)

    assert result["mode"] == "cross-ad-paid-fallback"
    assert attempted == [
        "always-free-a1",
        "cross-ad-free-a1",
        "paid-fallback",
        "cross-ad-paid-fallback",
    ]
    assert len(FakeCoordinator.instance.reserved) == 2
    assert FakeCoordinator.instance.released == ["reservation-1"]
    assert FakeCoordinator.instance.settled == []


def test_failed_paid_attempt_settles_full_reservation_before_retry(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    oci = RecoveryOci()
    attempted: list[str] = []

    def fake_launch_attempt(**kwargs):
        mode = kwargs["recovery_mode"]
        attempted.append(mode)
        if mode in {"always-free-a1", "cross-ad-free-a1"}:
            raise oci_recovery.CapacityUnavailable("OutOfHostCapacity")
        if mode == "paid-fallback":
            raise oci_recovery.RecoveryError("candidate health failed")
        return oci_recovery.CandidateAttempt(
            instance_id="ocid1.instance.crossad-paid",
            target=kwargs["target"],
            volume_id="ocid1.volume.crossad-paid",
            storage_mode="r2-restore",
            recovery_mode=mode,
            previous_instance_id=None,
            owns_recovery_volume=True,
            budget_reservation_id=kwargs["budget_reservation_id"],
            budget_reserved_microusd=kwargs["budget_reserved_microusd"],
        )

    monkeypatch.setattr(oci_recovery, "CoordinatorClient", FakeCoordinator)
    monkeypatch.setattr(oci_recovery, "_launch_attempt", fake_launch_attempt)

    result = oci_recovery.recover(_event(tmp_path), config, oci)

    assert result["mode"] == "cross-ad-paid-fallback"
    assert FakeCoordinator.instance.settled == [
        ("reservation-1", 1_200_000)
    ]


def test_paid_cleanup_stops_before_termination_when_budget_settlement_fails(
    monkeypatch,
    tmp_path,
) -> None:
    _base_env(monkeypatch, tmp_path)
    config = oci_recovery.RecoveryConfig.from_env()
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    class FakeOci:
        def __init__(self) -> None:
            self.softstopped: list[str] = []
            self.terminated: list[str] = []

        def list_instances(self, _config):
            return [
                {
                    "id": "paid-expired",
                    "lifecycle-state": "RUNNING",
                    "freeform-tags": {
                        "KatchaPaidFallback": "true",
                        "KatchaRecoveryMode": "paid-fallback",
                        "KatchaExpiresAt": (
                            now - timedelta(minutes=1)
                        ).isoformat(),
                        "KatchaBudgetReservationId": "reservation-1",
                        "KatchaBudgetReservedMicrousd": "1200000",
                        "KatchaPaidStartedAt": (
                            now - timedelta(hours=3)
                        ).isoformat(),
                        "KatchaEstimatedHourlyUsd": "0.10",
                    },
                }
            ]

        def softstop(self, instance_id):
            self.softstopped.append(instance_id)

        def terminate(self, instance_id):
            self.terminated.append(instance_id)

    class FailingCoordinator:
        def settle_external_compute(self, *_args, **_kwargs):
            raise oci_recovery.RecoveryError("coordinator unavailable")

    oci = FakeOci()
    with pytest.raises(
        oci_recovery.RecoveryError,
        match="coordinator unavailable",
    ):
        oci_recovery.cleanup_expired_paid(
            config,
            oci,
            FailingCoordinator(),
            now=now,
        )

    assert oci.softstopped == ["paid-expired"]
    assert oci.terminated == []


def test_paid_instance_cost_uses_runtime_and_caps_at_expiry() -> None:
    started = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    row = {
        "id": "paid",
        "freeform-tags": {
            "KatchaPaidFallback": "true",
            "KatchaBudgetReservationId": "reservation-1",
            "KatchaBudgetReservedMicrousd": "1200000",
            "KatchaPaidStartedAt": started.isoformat(),
            "KatchaEstimatedHourlyUsd": "0.10",
            "KatchaExpiresAt": (
                started + timedelta(hours=12)
            ).isoformat(),
        },
    }

    assert oci_recovery._paid_instance_cost_microusd(
        row,
        ended_at=started + timedelta(hours=2),
    ) == 200_000
    assert oci_recovery._paid_instance_cost_microusd(
        row,
        ended_at=started + timedelta(hours=20),
    ) == 1_200_000
