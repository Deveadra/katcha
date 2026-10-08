"""Recovery candidates get defined-tag identity and only four Vault secret grants."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from katcha.ops import oci_recovery

SOURCE = Path("scripts/print-oci-recovery-vault-iam.py")
SPEC = importlib.util.spec_from_file_location("katcha_recovery_iam_plan", SOURCE)
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)


def _variables() -> dict[str, str]:
    return {
        "KATCHA_OCI_COMPARTMENT_ID": "ocid1.compartment.test",
        "KATCHA_OCI_PRODUCTION_ENV_SECRET_ID": "ocid1.vaultsecret.test.production",
        "KATCHA_OCI_AWS_BUNDLE_SECRET_ID": "ocid1.vaultsecret.test.aws",
        "KATCHA_OCI_BACKUP_ENV_SECRET_ID": "ocid1.vaultsecret.test.backup",
        "KATCHA_OCI_RESTORE_ENV_SECRET_ID": "ocid1.vaultsecret.test.restore",
        "KATCHA_OCI_RECOVERY_CONFIGURED": "false",
        "KATCHA_OCI_PAID_FALLBACK_ENABLED": "false",
    }


def test_candidate_launch_attaches_defined_tag_before_cloud_init(tmp_path, monkeypatch):
    called = []
    oci = oci_recovery.OciCli()

    def capture(args):
        called.append(args)
        return {"data": {"id": "ocid1.instance.candidate"}}

    monkeypatch.setattr(oci, "run", capture)
    plan = oci_recovery.ShapePlan(
        mode="always-free-a1",
        shape="VM.Standard.A1.Flex",
        image_id="ocid1.image.test",
        ocpus=2,
        memory_gb=12,
        paid=False,
    )
    target = oci_recovery.RecoveryTarget(
        availability_domain="TEST:AD-1",
        subnet_id="ocid1.subnet.test",
    )
    config = SimpleNamespace(
        compartment_id="ocid1.compartment.test",
        assign_public_ip=False,
    )
    data = tmp_path / "user-data.sh"
    data.write_text("#!/bin/bash\n", encoding="utf-8")
    instance_id = oci.launch(
        config,
        oci_recovery.Incident("incident-1", 4, "oci-a1-primary-001"),
        deployment_id="oci-candidate-001",
        deployment_epoch=5,
        plan=plan,
        target=target,
        recovery_mode="always-free-a1",
        data_volume_id="ocid1.volume.test",
        storage_mode="existing-volume",
        user_data_path=data,
    )
    assert instance_id == "ocid1.instance.candidate"
    assert len(called) == 1
    argv = called[0]
    assert argv[:3] == ["compute", "instance", "launch"]
    assert json.loads(argv[argv.index("--defined-tags") + 1]) == {
        "KatchaRecovery": {"Candidate": "true"}
    }
    assert "--freeform-tags" in argv
    assert argv.index("--defined-tags") < argv.index("--user-data-file")


def test_iam_plan_scopes_membership_and_exact_four_secret_ids():
    data = planner.render(_variables())

    assert "All {instance.compartment.id = 'ocid1.compartment.test'" in data
    assert "tag.KatchaRecovery.Candidate.value = 'true'" in data
    assert "target.tag-namespace.name='KatchaRecovery'" in data
    assert "to read secret-bundles in compartment katcha-prod where any {" in data
    assert data.count("target.secret.id=") == 4
    assert "to manage secret-family" not in data
    assert "to manage vaults" not in data
    assert "INSPECT_ONLY" in data


@pytest.mark.parametrize(
    "modified",
    [
        {"KATCHA_OCI_COMPARTMENT_ID": ""},
        {"KATCHA_OCI_RESTORE_ENV_SECRET_ID": "not-an-ocid"},
        {"KATCHA_OCI_AWS_BUNDLE_SECRET_ID": "ocid1.vaultsecret.test.production"},
        {"KATCHA_OCI_RECOVERY_CONFIGURED": "true"},
        {"KATCHA_OCI_PAID_FALLBACK_ENABLED": "true"},
    ],
)
def test_iam_plan_refuses_incomplete_or_live_recovery_configuration(modified):
    data = _variables()
    data.update(modified)
    with pytest.raises(ValueError):
        planner.render(data)


def test_runbook_no_longer_recommends_compartment_wide_secret_reads():
    content = Path("docs/LIVE_CLOUD_CUTOVER.md").read_text(encoding="utf-8")
    assert "All {instance.compartment.id" in content
    assert "tag.KatchaRecovery.Candidate.value" in content
    assert "target.secret.id" in content
    assert "compartment-wide dynamic-group rule" not in content
