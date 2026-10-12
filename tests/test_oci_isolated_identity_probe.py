"""An OCI identity probe must remain separate from production authority and disks."""

import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path("scripts/oci-isolated-identity-probe.py")
SPEC = importlib.util.spec_from_file_location("katcha_isolated_identity_probe", SOURCE)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def plan():
    return {
        "compartment_id": "ocid1.compartment.test",
        "primary_id": "ocid1.instance.primary",
        "image_id": "ocid1.image.oraclelinux",
        "subnet_id": "ocid1.subnet.ad3",
        "availability_domain": "TEST:AD-3",
        "nsg_id": "ocid1.networksecuritygroup.admin",
    }


class FakeOci:
    def __init__(self):
        self.candidate = {
            "lifecycle-state": "RUNNING",
            "shape": probe.OCI_SHAPE,
            "display-name": "katcha-identity-probe-abcdef123456",
            "availability-domain": "TEST:AD-3",
            "freeform-tags": {
                "KatchaIsolationProbe": "true",
                "KatchaProduction": "false",
                "KatchaProbeId": "abcdef123456",
            },
            "defined-tags": {},
            "metadata": {"ssh_authorized_keys": "ssh-ed25519 PUBLIC"},
        }
        self.volume_attachments = []
        self.boot_attachments = [
            {"lifecycle-state": "ATTACHED", "boot-volume-id": "ocid1.bootvolume.test"}
        ]
        self.boot = {"size-in-gbs": 50}
        self.vnic_attachments = [{"lifecycle-state": "ATTACHED", "vnic-id": "ocid1.vnic.test"}]
        self.vnic = {
            "public-ip": None,
            "subnet-id": "ocid1.subnet.ad3",
            "nsg-ids": ["ocid1.networksecuritygroup.admin"],
        }
        self.primary = {"lifecycle-state": "RUNNING"}
        self.terminated = []
        self.boot_inventory = [{"id": "ocid1.bootvolume.test", "lifecycle-state": "AVAILABLE"}]

    def read(self, *args):
        key = args[:3]
        if key == ("compute", "instance", "get"):
            return self.primary if args[-1] == "ocid1.instance.primary" else self.candidate
        if key == ("compute", "volume-attachment", "list"):
            return self.volume_attachments
        if key == ("compute", "boot-volume-attachment", "list"):
            return self.boot_attachments
        if key == ("bv", "boot-volume", "get"):
            return self.boot
        if key == ("bv", "boot-volume", "list"):
            return self.boot_inventory
        if key == ("compute", "vnic-attachment", "list"):
            return self.vnic_attachments
        if key == ("network", "vnic", "get"):
            return self.vnic
        raise AssertionError(f"unexpected read: {key}")

    def terminate_test_instance(self, instance_id):
        self.terminated.append(instance_id)
        self.boot_inventory = []


def test_candidate_identity_tag_requires_exact_probe_and_preserves_existing_tags():
    class TagOci(FakeOci):
        def __init__(self):
            super().__init__()
            self.updated = []

        def update_probe_defined_tags(self, instance_id, defined_tags):
            self.updated.append((instance_id, defined_tags))
            self.candidate["defined-tags"] = defined_tags

    oci = TagOci()
    oci.candidate["defined-tags"] = {"ExistingNamespace": {"Keep": "untouched"}}
    result = probe.promote_identity_only_candidate(
        oci, plan(), "ocid1.instance.candidate"
    )
    assert result["status"] == "IDENTITY_PROBE_CANDIDATE_TAG_VERIFIED"
    assert result["vault_candidate_tag"] is True
    assert result["secret_access_tested"] is False
    assert oci.updated[0][0] == "ocid1.instance.candidate"
    assert oci.updated[0][1] == {
        "ExistingNamespace": {"Keep": "untouched"},
        "KatchaRecovery": {"Candidate": "true"},
    }
    assert probe.verify(
        oci, plan(), "ocid1.instance.candidate", for_cleanup=True
    )["vault_candidate_tag"] is True
    with pytest.raises(probe.ProbeError):
        probe.promote_identity_only_candidate(
            oci, plan(), "ocid1.instance.candidate"
        )
    assert len(oci.updated) == 1


def test_candidate_identity_tag_rejects_wrong_name_and_prod_instance():
    class TagOci(FakeOci):
        def update_probe_defined_tags(self, *_args):
            pytest.fail("tag mutation must not happen")

    oci = TagOci()
    oci.candidate["display-name"] = "unrecognized-instance"
    with pytest.raises(probe.ProbeError, match="exact isolation probe identity"):
        probe.promote_identity_only_candidate(
            oci, plan(), "ocid1.instance.candidate"
        )
    with pytest.raises(probe.ProbeError, match="isolated instance contract"):
        probe.promote_identity_only_candidate(
            oci, plan(), "ocid1.instance.primary"
        )


def test_candidate_identity_tag_refuses_existing_defined_candidate_field():
    class TagOci(FakeOci):
        def update_probe_defined_tags(self, *_args):
            pytest.fail("tag mutation must not happen")

    oci = TagOci()
    oci.candidate["defined-tags"] = {"KatchaRecovery": {"Candidate": "false"}}
    with pytest.raises(probe.ProbeError, match="already defined"):
        probe.promote_identity_only_candidate(
            oci, plan(), "ocid1.instance.candidate"
        )


def test_candidate_tag_cli_never_changes_freeform_tags(monkeypatch):
    seen = []

    def fake_command(args, *, timeout=90):
        seen.append((args, timeout))
        return "{}"

    monkeypatch.setattr(probe, "command", fake_command)
    oci = probe.Oci.__new__(probe.Oci)
    oci.base = ["oci", "--profile", "KATCHA_VAULT_ADMIN"]
    oci.update_probe_defined_tags(
        "ocid1.instance.candidate",
        {"KatchaRecovery": {"Candidate": "true"}},
    )
    cmd, timeout = seen[0]
    assert cmd[:3] == ["oci", "--profile", "KATCHA_VAULT_ADMIN"]
    assert cmd[3:6] == ["compute", "instance", "update"]
    assert cmd[cmd.index("--instance-id") + 1] == "ocid1.instance.candidate"
    assert json.loads(cmd[cmd.index("--defined-tags") + 1]) == {
        "KatchaRecovery": {"Candidate": "true"}
    }
    assert "--freeform-tags" not in cmd
    assert "--force" in cmd
    assert timeout == 180


def test_tag_candidate_cli_requires_separate_scoped_flags(monkeypatch, capsys):
    monkeypatch.setattr(probe, "Oci", lambda: object())
    monkeypatch.setattr(probe, "gather", lambda *_args, **_kwargs: plan())
    assert probe.main([
        "tag-candidate", "--instance-id", "ocid1.instance.candidate"
    ]) == 2
    assert "separate IAM-tag approval" in capsys.readouterr().err


def test_launch_command_contains_only_isolated_probe_inputs(tmp_path):
    key = tmp_path / "public.key"
    args = probe.launch_args(plan(), key, "abcdef123456")
    assert args[args.index("--shape") + 1] == probe.OCI_SHAPE
    assert args[args.index("--boot-volume-size-in-gbs") + 1] == "50"
    assert args[args.index("--assign-public-ip") + 1] == "false"
    assert json.loads(args[args.index("--nsg-ids") + 1]) == [plan()["nsg_id"]]
    tags = json.loads(args[args.index("--freeform-tags") + 1])
    assert tags["KatchaIsolationProbe"] == "true"
    assert tags["KatchaProduction"] == "false"
    assert "--defined-tags" not in args
    assert "--user-data-file" not in args
    assert "--source-boot-volume-id" not in args
    assert "--volume-id" not in args


def test_verify_confirms_running_private_diskless_candidate_and_primary():
    result = probe.verify(FakeOci(), plan(), "ocid1.instance.candidate")
    assert result == {
        "status": "ISOLATED_INSTANCE_VERIFIED",
        "instance_id": "ocid1.instance.candidate",
        "primary_running": True,
        "public_ip": False,
        "data_volumes": 0,
        "production_bootstrap": False,
        "vault_candidate_tag": False,
        "recovery_enabled": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda o: o.candidate["defined-tags"].update({"KatchaRecovery": {"Candidate": "true"}}),
        lambda o: o.candidate["metadata"].update({"user_data": "cloud-init"}),
        lambda o: o.candidate.update({"shape": "VM.Standard.A1.Flex"}),
        lambda o: o.volume_attachments.append({"lifecycle-state": "ATTACHED"}),
        lambda o: o.boot.update({"size-in-gbs": 100}),
        lambda o: o.vnic.update({"public-ip": "203.0.113.1"}),
        lambda o: o.vnic.update({"subnet-id": "ocid1.subnet.prod"}),
        lambda o: o.primary.update({"lifecycle-state": "STOPPED"}),
    ],
)
def test_verify_rejects_candidate_or_primary_isolation_failure(change):
    oci = FakeOci()
    change(oci)
    with pytest.raises(probe.ProbeError):
        probe.verify(oci, plan(), "ocid1.instance.candidate")


@pytest.mark.parametrize(
    "source,port,allowed",
    [
        ("10.42.2.156/32", 22, True),
        ("0.0.0.0/0", 22, False),
        ("10.42.3.0/24", 22, False),
        ("10.42.2.156/32", 5432, False),
    ],
)
def test_admin_nsg_allows_only_bastion_ssh(source, port, allowed):
    rules = [
        {
            "direction": "INGRESS",
            "protocol": "6",
            "source": source,
            "tcp-options": {"destination-port-range": {"min": port, "max": port}},
        }
    ]
    assert probe._ssh_only_from_bastion(rules, "10.42.2.156") is allowed


def test_admin_nsg_source_must_match_current_bastion_ip():
    rules = [
        {
            "direction": "INGRESS",
            "protocol": "6",
            "source": "10.42.2.155/32",
            "tcp-options": {"destination-port-range": {"min": 22, "max": 22}},
        }
    ]
    assert not probe._ssh_only_from_bastion(rules, "10.42.2.156")


def test_oci_read_rejects_mutation_before_running_subprocess():
    oci = object.__new__(probe.Oci)
    oci.base = ["oci"]
    with pytest.raises(probe.ProbeError, match="not allowlisted"):
        oci.read("compute", "instance", "terminate", "--instance-id", "primary")


def test_cli_needs_explicit_storage_and_launch_flag(monkeypatch, capsys):
    class NoLaunch:
        def launch(self, _args):
            raise AssertionError("launch must not run")

    monkeypatch.setattr(probe, "Oci", NoLaunch)
    monkeypatch.setattr(probe, "gather", lambda *_args, **_kwargs: plan())
    monkeypatch.setattr("sys.argv", ["probe", "launch"])
    assert probe.main() == 2
    assert "requires explicit approval" in capsys.readouterr().err


def test_cleanup_removes_only_exact_tagged_test_instance_and_new_boot():
    oci = FakeOci()
    result = probe.cleanup(oci, plan(), "ocid1.instance.candidate")
    assert oci.terminated == ["ocid1.instance.candidate"]
    assert result["status"] == "TEST_RESOURCES_REMOVED"
    assert result["test_boot_volume_removed"] is True
    assert result["primary_running"] is True


def test_cleanup_rejects_name_tag_mismatch_before_mutation():
    oci = FakeOci()
    oci.candidate["display-name"] = "katcha-prod-primary-001"
    with pytest.raises(probe.ProbeError, match="not the exact probe"):
        probe.cleanup(oci, plan(), "ocid1.instance.candidate")
    assert oci.terminated == []


def test_cleanup_remains_available_if_primary_is_down():
    oci = FakeOci()
    oci.primary["lifecycle-state"] = "STOPPED"
    result = probe.cleanup(oci, plan(), "ocid1.instance.candidate")
    assert result["status"] == "TEST_RESOURCES_REMOVED"
    assert result["primary_running"] is False


def test_cleanup_revokes_candidate_vault_tag_before_termination():
    class TaggedOci(FakeOci):
        def __init__(self):
            super().__init__()
            self.updated = []
            self.candidate["defined-tags"] = {
                "ExistingNamespace": {"Keep": "safe"},
                "KatchaRecovery": {"Candidate": "true", "Other": "preserved"},
            }

        def update_probe_defined_tags(self, instance_id, defined_tags):
            assert self.terminated == []
            self.updated.append((instance_id, defined_tags))
            self.candidate["defined-tags"] = defined_tags

    oci = TaggedOci()
    result = probe.cleanup(oci, plan(), "ocid1.instance.candidate")
    assert result["status"] == "TEST_RESOURCES_REMOVED"
    assert oci.terminated == ["ocid1.instance.candidate"]
    assert oci.updated == [
        (
            "ocid1.instance.candidate",
            {
                "ExistingNamespace": {"Keep": "safe"},
                "KatchaRecovery": {"Other": "preserved"},
            },
        )
    ]


def test_cleanup_refuses_termination_when_tag_revocation_unverified():
    class UncertainOci(FakeOci):
        def __init__(self):
            super().__init__()
            self.candidate["defined-tags"] = {"KatchaRecovery": {"Candidate": "true"}}

        def update_probe_defined_tags(self, *_args):
            pass  # Simulate response succeeded but tag was not removed.

    oci = UncertainOci()
    with pytest.raises(probe.ProbeError, match="revocation unverified"):
        probe.cleanup(oci, plan(), "ocid1.instance.candidate")
    assert oci.terminated == []


def test_cli_cleanup_requires_separate_explicit_flag(monkeypatch, capsys):
    class NoCleanup:
        def terminate_test_instance(self, _id):
            raise AssertionError("termination must not run")

    monkeypatch.setattr(probe, "Oci", NoCleanup)
    monkeypatch.setattr(probe, "cleanup_plan", plan)
    monkeypatch.setattr("sys.argv", ["probe", "cleanup", "--instance-id", "ocid1.instance.test"])
    assert probe.main() == 2
    assert "requires exact instance ID" in capsys.readouterr().err
