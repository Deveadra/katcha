"""Read-only OCI candidate capacity inventory: never confuse slots with cost safety."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PATH = Path("scripts/audit-oci-free-candidate-capacity.py")
SPEC = importlib.util.spec_from_file_location("oci_candidate_capacity", PATH)
assert SPEC and SPEC.loader
candidate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = candidate
SPEC.loader.exec_module(candidate)

TENANCY = "ocid1.tenancy.test"
PRODUCTION = "ocid1.compartment.test"
PRIMARY = "ocid1.instance.primary"
AD = "CAAS:ASHBURN-AD-1"


def fake_inventory(*, micros=0, a1_ocpus=2, a1_memory=12,
                   boot_gb=50, block_gb=50, state="RUNNING",
                   home=True, micro_shape=True, missing_memory=False,
                   bad_volume=False):
    commands = []

    def fetch(argv):
        cmd = " ".join(argv)
        commands.append(argv)
        if "region-subscription list" in cmd:
            return {"data": [{
                "is-home-region": home, "region-name": "us-ashburn-1"
            }]}
        if "compartment list" in cmd:
            assert "--compartment-id-in-subtree true" in cmd
            assert "--access-level ANY" in cmd
            return {"data": [{"id": PRODUCTION, "lifecycle-state": "ACTIVE"}]}
        if "availability-domain list" in cmd:
            return {"data": [{"name": AD}]}
        if "compute instance list" in cmd:
            if TENANCY in argv:
                return {"data": []}
            if PRODUCTION in argv:
                instances = [{
                    "id": PRIMARY,
                    "lifecycle-state": state,
                    "shape": candidate.A1_SHAPE,
                    "shape-config": {
                        "ocpus": a1_ocpus,
                        **({} if missing_memory else {"memory-in-gbs": a1_memory}),
                    },
                }]
                for idx in range(micros):
                    instances.append({
                        "id": f"ocid1.instance.micro{idx}",
                        "shape": candidate.MICRO_SHAPE,
                        "lifecycle-state": "STOPPED",
                    })
                return {"data": instances}
        if "compute instance get" in cmd:
            return {"data": {"shape-config": {"ocpus": a1_ocpus, "memory-in-gbs": a1_memory}}}
        if "bv boot-volume list" in cmd:
            return {"data": []} if TENANCY in argv else {"data": [{
                "id": "ocid1.bootvolume.test",
                "size-in-gbs": None if bad_volume else boot_gb,
                "lifecycle-state": "AVAILABLE",
            }]}
        if "bv volume list" in cmd:
            return {"data": []} if TENANCY in argv else {"data": [{
                "id": "ocid1.volume.test",
                "size-in-gbs": block_gb,
                "lifecycle-state": "AVAILABLE",
            }]}
        if "compute shape list" in cmd:
            assert "--shape VM.Standard.E2.1.Micro" in cmd
            return {"data": [{"shape": candidate.MICRO_SHAPE}]} if micro_shape else {"data": []}
        raise AssertionError("Unexpected OCI operation: " + cmd)

    return fetch, commands


def run_inventory(**kwargs):
    fetch, commands = fake_inventory(**kwargs)
    return candidate.inventory(fetch, TENANCY, "us-ashburn-1", PRODUCTION, PRIMARY), commands


def test_full_active_a1_free_limit_does_not_preclude_separate_free_micro():
    r, commands = run_inventory()
    assert r.primary_running
    assert r.a1_ocpus == 2
    assert r.a1_memory_gb == 12
    assert r.a1_headroom_ocpus == 0
    assert r.a1_headroom_memory_gb == 0
    assert r.micros == 0 and r.micro_slots_left == 2
    assert r.boot_and_block_gb == 100
    assert r.free_storage_gb == 100
    assert r.micro_probe_candidate
    assert r.micro_availability_domains == (AD,)
    assert all("launch" not in cmd for cmd in commands)
    assert all("delete" not in cmd for cmd in commands)
    assert all("secret-bundle" not in " ".join(cmd) for cmd in commands)


@pytest.mark.parametrize("kwargs", [
    {"micros": 2},
    {"boot_gb": 151},
    {"block_gb": 160},
    {"micro_shape": False},
])
def test_no_micro_probe_eligibility_when_slot_storage_or_shape_unavailable(kwargs):
    r, _ = run_inventory(**kwargs)
    assert not r.micro_probe_candidate


def test_stopped_micro_still_consumes_free_instance_slot():
    r, _ = run_inventory(micros=1)
    assert r.micros == 1
    assert r.micro_slots_left == 1


def test_incomplete_live_metadata_fails_closed():
    with pytest.raises(ValueError, match="production primary"):
        run_inventory(state="STOPPED")
    with pytest.raises(ValueError, match="unrecognized A1 memory"):
        run_inventory(missing_memory=True)
    with pytest.raises(ValueError, match="unrecognized volume size"):
        run_inventory(bad_volume=True)
    with pytest.raises(ValueError, match="home region"):
        run_inventory(home=False)


def test_no_production_instance_id_is_not_allowed():
    fetch, _ = fake_inventory()
    with pytest.raises(ValueError, match="primary IDs"):
        candidate.inventory(fetch, TENANCY, "us-ashburn-1", PRODUCTION, "")


def test_terminated_primary_fails_closed():
    with pytest.raises(ValueError, match="production primary"):
        run_inventory(state="TERMINATED")


def test_pending_termination_still_consumes_capacity():
    assert candidate._active({"lifecycle-state": "TERMINATING"})
    assert not candidate._active({"lifecycle-state": "TERMINATED"})


class MockAuditError(Exception):
    pass


class MockOciAudit:
    AuditError = MockAuditError

    def __init__(self, fail_session=False, fail_metadata=False):
        self.fail_session = fail_session
        self.fail_metadata = fail_metadata
        self.commands = []

    def read_command(self, argv):
        self.commands.append(tuple(argv))
        if self.fail_session:
            raise self.AuditError("opaque OCI stderr containing private material")
        return "Valid until ..."

    def metadata(self, argv, *, empty_list_ok=False):
        self.commands.append(tuple(argv))
        if self.fail_metadata:
            raise self.AuditError("opaque OCI provider error")
        return {"data": []}


def test_expired_session_message_not_bastion_and_no_credentials():
    provider = MockOciAudit(fail_session=True)
    with pytest.raises(ValueError, match="OCI_ADMIN_SESSION_UNAVAILABLE") as raised:
        candidate.validate_admin_session(
            provider, ["oci", "--profile", "KATCHA_VAULT_ADMIN"]
        )
    message = str(raised.value)
    assert "Bastion" in message
    assert "private material" not in message
    assert len(provider.commands) == 1
    assert provider.commands[0][-2:] == ("session", "validate")


def test_valid_session_proceeds_without_changing_resources():
    provider = MockOciAudit()
    candidate.validate_admin_session(provider, ["oci"])
    assert provider.commands == [("oci", "session", "validate")]


@pytest.mark.parametrize(
    "cmd,stage",
    [
        (["iam", "region-subscription", "list", "--tenancy-id", "ocid1.tenancy.test",
          "--output", "json"], "iam region-subscription list"),
        (["compute", "instance", "list", "--compartment-id", "ocid1.compartment.test",
          "--all", "--output", "json"], "compute instance list"),
        (["bv", "boot-volume", "list", "--compartment-id", "ocid1.compartment.test",
          "--all", "--output", "json"], "bv boot-volume list"),
    ],
)
def test_inventory_read_failure_includes_only_safe_stage(cmd, stage):
    provider = MockOciAudit(fail_metadata=True)
    with pytest.raises(ValueError, match="OCI_READ_FAILED") as raised:
        candidate.read_oci_metadata(provider, ["oci"], cmd)
    message = str(raised.value)
    assert stage in message
    assert "opaque" not in message
    assert "ocid1" not in message
    assert len(provider.commands) == 1
    assert provider.commands[0][-1] == "json"


def test_successful_readonly_metadata_query_retains_empty_list_semantics():
    provider = MockOciAudit()
    result = candidate.read_oci_metadata(
        provider,
        ["oci"],
        ["compute", "instance", "list", "--all", "--output", "json"],
    )
    assert result == {"data": []}
    assert provider.commands[0][:4] == ("oci", "compute", "instance", "list")


def test_inventory_metadata_rejects_missing_json_output_flag():
    provider = MockOciAudit()
    with pytest.raises(ValueError, match="must request JSON"):
        candidate.read_oci_metadata(
            provider,
            ["oci"],
            ["compute", "instance", "launch", "--display-name", "unsafe"],
        )
    assert not provider.commands
