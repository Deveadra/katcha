from __future__ import annotations

import json
from typing import Any

import pytest

from katcha.ops import oci_bootstrap_capacity as bootstrap


def _config() -> bootstrap.BootstrapConfig:
    return bootstrap.BootstrapConfig(
        compartment_id="ocid1.compartment.test",
        image_id="ocid1.image.arm",
        ssh_public_key="ssh-ed25519 AAAATEST katcha-prod",
        targets=(
            bootstrap.BootstrapTarget(
                "TEST:AD-1",
                "ocid1.subnet.ad1",
                "katcha-prod-data",
            ),
            bootstrap.BootstrapTarget(
                "TEST:AD-2",
                "ocid1.subnet.ad2",
                "katcha-prod-data-ad2",
            ),
            bootstrap.BootstrapTarget(
                "TEST:AD-3",
                "ocid1.subnet.ad3",
                "katcha-prod-data-ad3",
            ),
        ),
        primary_volume_id="",
    )


def _instance(
    *,
    instance_id: str = "ocid1.instance.test",
    ad: str = "TEST:AD-2",
    subnet: str = "ocid1.subnet.ad2",
) -> dict[str, Any]:
    return {
        "id": instance_id,
        "display-name": "katcha-prod-primary-001",
        "availability-domain": ad,
        "lifecycle-state": "RUNNING",
        "shape": "VM.Standard.A1.Flex",
        "shape-config": {
            "ocpus": 2,
            "memory-in-gbs": 12,
        },
        "freeform-tags": {
            "KatchaRole": "control-plane",
            "KatchaSubnetId": subnet,
        },
    }


def _base_env(monkeypatch) -> None:
    values = {
        "KATCHA_OCI_COMPARTMENT_ID": "ocid1.compartment.test",
        "KATCHA_OCI_PRIMARY_IMAGE_ID": "ocid1.image.arm",
        "KATCHA_OCI_SSH_PUBLIC_KEY": "ssh-ed25519 AAAATEST katcha-prod",
        "KATCHA_OCI_AVAILABILITY_DOMAIN": "TEST:AD-1",
        "KATCHA_OCI_SUBNET_ID": "ocid1.subnet.ad1",
        "KATCHA_OCI_CROSS_AD_TARGETS_JSON": json.dumps(
            [
                {
                    "availability_domain": "TEST:AD-2",
                    "subnet_id": "ocid1.subnet.ad2",
                },
                {
                    "availability_domain": "TEST:AD-3",
                    "subnet_id": "ocid1.subnet.ad3",
                },
            ]
        ),
        "KATCHA_OCI_PRIMARY_SHAPE": "VM.Standard.A1.Flex",
        "KATCHA_OCI_PRIMARY_OCPUS": "2",
        "KATCHA_OCI_PRIMARY_MEMORY_GB": "12",
        "KATCHA_OCI_ASSIGN_PUBLIC_IP": "false",
        "KATCHA_OCI_CROSS_AD_DATA_VOLUME_SIZE_GB": "50",
        "KATCHA_OCI_DATA_VOLUME_DEVICE_PATH": "/dev/oracleoci/oraclevdb",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_bootstrap_config_keeps_full_a1_target_and_all_ads(monkeypatch) -> None:
    _base_env(monkeypatch)

    config = bootstrap.BootstrapConfig.from_env()

    assert config.shape == "VM.Standard.A1.Flex"
    assert config.ocpus == 2
    assert config.memory_gb == 12
    assert [target.availability_domain for target in config.targets] == [
        "TEST:AD-1",
        "TEST:AD-2",
        "TEST:AD-3",
    ]
    assert [target.volume_name for target in config.targets] == [
        "katcha-prod-data",
        "katcha-prod-data-ad2",
        "katcha-prod-data-ad3",
    ]


def test_bootstrap_config_refuses_accidental_downsize(monkeypatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("KATCHA_OCI_PRIMARY_OCPUS", "1")
    monkeypatch.setenv("KATCHA_OCI_PRIMARY_MEMORY_GB", "6")

    with pytest.raises(bootstrap.BootstrapError, match="2 OCPU / 12 GB"):
        bootstrap.BootstrapConfig.from_env()


class CapacityOnlyClient:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, args: list[str]) -> dict[str, Any]:
        self.commands.append(list(args))
        if args[:3] == ["compute", "instance", "list"]:
            return {"data": []}
        if args[:3] == ["compute", "instance", "launch"]:
            raise bootstrap.CapacityUnavailable("Out of host capacity.")
        raise AssertionError(f"unexpected OCI command: {args}")


def test_capacity_poll_tries_every_ad_without_creating_storage() -> None:
    client = CapacityOnlyClient()

    result = bootstrap.BootstrapProvisioner(
        _config(),
        client,
        sleep=lambda _seconds: None,
    ).acquire()

    assert result["status"] == "capacity-unavailable"
    assert result["attempted"] == ["TEST:AD-1", "TEST:AD-2", "TEST:AD-3"]
    launches = [
        command
        for command in client.commands
        if command[:3] == ["compute", "instance", "launch"]
    ]
    assert len(launches) == 3
    assert not any(command and command[0] == "bv" for command in client.commands)


class Ad2SuccessClient:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.instance = _instance()

    def run(self, args: list[str]) -> dict[str, Any]:
        self.commands.append(list(args))
        prefix = args[:3]
        if prefix == ["compute", "instance", "list"]:
            return {"data": []}
        if prefix == ["compute", "instance", "launch"]:
            ad = args[args.index("--availability-domain") + 1]
            if ad == "TEST:AD-1":
                raise bootstrap.CapacityUnavailable("Out of host capacity.")
            assert ad == "TEST:AD-2"
            assert args[args.index("--assign-public-ip") + 1] == "false"
            assert json.loads(args[args.index("--shape-config") + 1]) == {
                "ocpus": 2.0,
                "memoryInGBs": 12.0,
            }
            return {"data": dict(self.instance)}
        if prefix == ["compute", "vnic-attachment", "list"]:
            return {
                "data": [
                    {
                        "lifecycle-state": "ATTACHED",
                        "vnic-id": "ocid1.vnic.test",
                    }
                ]
            }
        if prefix == ["network", "vnic", "get"]:
            return {
                "data": {
                    "public-ip": None,
                    "private-ip": "10.42.2.10",
                    "subnet-id": "ocid1.subnet.ad2",
                }
            }
        if prefix == ["bv", "volume", "list"]:
            return {"data": []}
        if prefix == ["bv", "volume", "create"]:
            assert args[args.index("--availability-domain") + 1] == "TEST:AD-2"
            return {
                "data": {
                    "id": "ocid1.volume.ad2",
                    "display-name": "katcha-prod-data-ad2",
                    "availability-domain": "TEST:AD-2",
                    "lifecycle-state": "AVAILABLE",
                }
            }
        if prefix == ["compute", "volume-attachment"]:
            operation = args[2]
            if operation == "list":
                return {"data": []}
            if operation == "attach":
                assert (
                    args[args.index("--device") + 1]
                    == "/dev/oracleoci/oraclevdb"
                )
                return {
                    "data": {
                        "id": "ocid1.volumeattachment.test",
                        "lifecycle-state": "ATTACHED",
                    }
                }
        if prefix == ["compute", "instance", "update"]:
            tags = json.loads(args[args.index("--freeform-tags") + 1])
            assert tags["KatchaDataVolumeId"] == "ocid1.volume.ad2"
            return {"data": dict(self.instance)}
        raise AssertionError(f"unexpected OCI command: {args}")


def test_capacity_poll_provisions_storage_only_after_ad2_launch() -> None:
    client = Ad2SuccessClient()

    result = bootstrap.BootstrapProvisioner(
        _config(),
        client,
        sleep=lambda _seconds: None,
    ).acquire()

    assert result["status"] == "acquired"
    assert result["availability_domain"] == "TEST:AD-2"
    assert result["volume_id"] == "ocid1.volume.ad2"
    assert result["attempted"] == ["TEST:AD-1", "TEST:AD-2"]
    assert result["reused_instance"] is False

    launches = [
        command
        for command in client.commands
        if command[:3] == ["compute", "instance", "launch"]
    ]
    assert len(launches) == 2


class UncertainLaunchClient(Ad2SuccessClient):
    def __init__(self) -> None:
        super().__init__()
        self.instance_lists = 0

    def run(self, args: list[str]) -> dict[str, Any]:
        if args[:3] == ["compute", "instance", "list"]:
            self.commands.append(list(args))
            self.instance_lists += 1
            if self.instance_lists == 1:
                return {"data": []}
            return {"data": [dict(self.instance)]}
        if args[:3] == ["compute", "instance", "launch"]:
            self.commands.append(list(args))
            raise bootstrap.BootstrapError("connection dropped after launch")
        return super().run(args)


def test_uncertain_launch_reconciles_before_trying_another_ad() -> None:
    client = UncertainLaunchClient()

    result = bootstrap.BootstrapProvisioner(
        _config(),
        client,
        sleep=lambda _seconds: None,
    ).acquire()

    assert result["status"] == "acquired"
    assert result["availability_domain"] == "TEST:AD-2"
    launches = [
        command
        for command in client.commands
        if command[:3] == ["compute", "instance", "launch"]
    ]
    assert len(launches) == 1
