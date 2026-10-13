#!/usr/bin/env python3
"""Plan, launch, and inspect one isolated OCI identity probe.

The launch subcommand is a resource mutation. Use only after explicit approval
of the concrete plan and temporary exhaustion of free storage headroom. This
tool never reads Vault bundles, starts Katcha services, or changes authority.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

OCI_SHAPE = "VM.Standard.E2.1.Micro"
IMAGE_NAME = "Oracle-Linux-9.8-2026.09.18-0"
BOOT_GB = 50
FREE_STORAGE_GB = 200
FREE_MICROS = 2
REPO = "Deveadra/katcha"
SWITCHES = (
    "KATCHA_OCI_RECOVERY_CONFIGURED",
    "KATCHA_EXTERNAL_COMPUTE_ENABLED",
    "KATCHA_OCI_PAID_FALLBACK_ENABLED",
)


class ProbeError(RuntimeError):
    pass


def command(argv: list[str], *, timeout: int = 90) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("probe metadata command unavailable or timed out") from exc
    if result.returncode != 0:
        # OCI and SSH stderr can include identifiers and credentials. Do not echo it.
        raise ProbeError("probe metadata command failed: " + " ".join(argv[:3]))
    return result.stdout


class Oci:
    def __init__(self) -> None:
        cli = Path.home() / ".cache/katcha/oci-cli-3.94.1/bin/oci"
        if not cli.is_file():
            raise ProbeError("isolated OCI CLI unavailable")
        self.base = [
            str(cli),
            "--profile",
            "KATCHA_VAULT_ADMIN",
            "--auth",
            "security_token",
            "--region",
            "us-ashburn-1",
        ]

    def read(self, *args: str) -> Any:
        allowed = {
            ("iam", "region-subscription", "list"),
            ("iam", "compartment", "list"),
            ("iam", "availability-domain", "list"),
            ("compute", "instance", "get"),
            ("compute", "instance", "list"),
            ("compute", "shape", "list"),
            ("compute", "image", "list"),
            ("compute", "vnic-attachment", "list"),
            ("compute", "boot-volume-attachment", "list"),
            ("compute", "volume-attachment", "list"),
            ("network", "subnet", "get"),
            ("network", "security-list", "get"),
            ("network", "vnic", "get"),
            ("bv", "boot-volume", "list"),
            ("bv", "boot-volume", "get"),
            ("bv", "volume", "list"),
            ("bastion", "bastion", "get"),
            ("organizations", "subscription", "list"),
        }
        prefix = tuple(args[:3])
        if prefix not in allowed and tuple(args[:4]) != ("network", "nsg", "rules", "list"):
            raise ProbeError("OCI read request is not allowlisted")
        try:
            raw = command([*self.base, *args, "--output", "json"])
            if not raw.strip() and args[2] == "list":
                return []
            return json.loads(raw)["data"]
        except (ValueError, KeyError) as exc:
            raise ProbeError("OCI returned invalid " + " ".join(args[:3]) + " metadata") from exc

    def launch(self, args: list[str]) -> dict[str, Any]:
        try:
            data = json.loads(
                command(
                    [*self.base, "compute", "instance", "launch", *args, "--output", "json"],
                    timeout=1300,
                )
            )["data"]
        except (ValueError, KeyError) as exc:
            raise ProbeError(
                "OCI launch returned invalid metadata; reconcile by probe tag"
            ) from exc
        if not isinstance(data, dict):
            raise ProbeError("OCI launch returned invalid instance metadata")
        return data

    def update_probe_defined_tags(self, instance_id: str, defined_tags: dict[str, Any]) -> None:
        """Only after caller proves exact probe identity and mutation approval."""
        try:
            command(
                [
                    *self.base,
                    "compute",
                    "instance",
                    "update",
                    "--instance-id",
                    instance_id,
                    "--defined-tags",
                    json.dumps(defined_tags, separators=(",", ":")),
                    "--force",
                    "--output",
                    "json",
                ],
                timeout=180,
            )
        except ProbeError as exc:
            raise ProbeError(
                "candidate tag outcome uncertain; inspect exact probe before retry"
            ) from exc

    def terminate_test_instance(self, instance_id: str) -> None:
        try:
            command(
                [
                    *self.base,
                    "compute",
                    "instance",
                    "terminate",
                    "--instance-id",
                    instance_id,
                    "--preserve-boot-volume",
                    "false",
                    "--force",
                    "--wait-for-state",
                    "SUCCEEDED",
                    "--max-wait-seconds",
                    "1200",
                ],
                timeout=1300,
            )
        except ProbeError as exc:
            raise ProbeError(
                "probe cleanup outcome uncertain; inspect exact instance and boot volume"
            ) from exc


def variable(name: str) -> str:
    return command(["gh", "variable", "get", name, "-R", REPO]).strip()


def _rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ProbeError(f"invalid {label} metadata")
    return value


def _active(row: dict[str, Any]) -> bool:
    return row.get("lifecycle-state") not in {"TERMINATED", "DELETED"}


def _no_ingress(oci: Oci, subnet: dict[str, Any]) -> bool:
    ids = subnet.get("security-list-ids")
    if not isinstance(ids, list) or not ids:
        return False
    return all(
        not oci.read("network", "security-list", "get", "--security-list-id", item).get(
            "ingress-security-rules"
        )
        for item in ids
    )


def _ssh_only_from_bastion(rules: list[dict[str, Any]], bastion_ip: str) -> bool:
    ingress = [row for row in rules if row.get("direction") == "INGRESS"]
    if len(ingress) != 1:
        return False
    rule = ingress[0]
    tcp = rule.get("tcp-options") or {}
    ports = tcp.get("destination-port-range") or {}
    source = rule.get("source")
    return (
        rule.get("protocol") == "6"
        and re.fullmatch(r"10\.42\.2\.\d{1,3}", bastion_ip) is not None
        and source == bastion_ip + "/32"
        and ports.get("min") == ports.get("max") == 22
    )


def gather(oci: Oci, *, for_new_launch: bool = True) -> dict[str, Any]:
    variables = {
        name: variable(name)
        for name in (
            *SWITCHES,
            "OCI_REGION",
            "KATCHA_OCI_COMPARTMENT_ID",
            "KATCHA_OCI_PRIMARY_INSTANCE_ID",
            "KATCHA_OCI_SUBNET_ID",
            "KATCHA_OCI_ADMIN_NSG_ID",
            "KATCHA_OCI_BASTION_ID",
            "KATCHA_OCI_CROSS_AD_TARGETS_JSON",
            "KATCHA_OCI_SSH_PUBLIC_KEY",
        )
    }
    if any(variables[key].lower() != "false" for key in SWITCHES):
        raise ProbeError("recovery or spend switch is enabled")
    if variables["OCI_REGION"] != "us-ashburn-1":
        raise ProbeError("unexpected OCI region")
    import configparser

    config = configparser.ConfigParser(interpolation=None)
    if not config.read(Path.home() / ".oci/config"):
        raise ProbeError("OCI administrator session config unavailable")
    tenancy = config.get("KATCHA_VAULT_ADMIN", "tenancy", fallback="")
    production = variables["KATCHA_OCI_COMPARTMENT_ID"]
    if not tenancy.startswith("ocid1.tenancy.") or not production.startswith("ocid1.compartment."):
        raise ProbeError("invalid tenancy or production compartment metadata")
    subscriptions = oci.read(
        "organizations", "subscription", "list", "--compartment-id", tenancy, "--all"
    )
    if (
        not isinstance(subscriptions, dict)
        or not isinstance(subscriptions.get("items"), list)
        or not any(
            isinstance(item, dict)
            and item.get("lifecycle-state") == "ACTIVE"
            and item.get("payment-model") == "FREE_TRIAL"
            for item in subscriptions["items"]
        )
    ):
        raise ProbeError("active Free Trial subscription is not verified")
    regions = _rows(
        oci.read("iam", "region-subscription", "list", "--tenancy-id", tenancy), "region"
    )
    if [r.get("region-name") for r in regions if r.get("is-home-region") is True] != [
        "us-ashburn-1"
    ]:
        raise ProbeError("us-ashburn-1 is not verified as the tenancy home region")
    compartments = [tenancy]
    children = _rows(
        oci.read(
            "iam",
            "compartment",
            "list",
            "--compartment-id",
            tenancy,
            "--compartment-id-in-subtree",
            "true",
            "--access-level",
            "ANY",
            "--all",
        ),
        "compartment",
    )
    compartments += [r["id"] for r in children if r.get("lifecycle-state") == "ACTIVE"]
    if production not in compartments:
        raise ProbeError("production compartment missing from inventory")
    ads = [
        r.get("name")
        for r in _rows(
            oci.read("iam", "availability-domain", "list", "--compartment-id", tenancy, "--all"),
            "AD",
        )
    ]
    if not ads or any(not isinstance(ad, str) or not ad for ad in ads):
        raise ProbeError("availability-domain inventory incomplete")
    try:
        targets = json.loads(variables["KATCHA_OCI_CROSS_AD_TARGETS_JSON"])
        target = next(t for t in targets if t["availability_domain"].endswith("AD-3"))
        ad, subnet_id = target["availability_domain"], target["subnet_id"]
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise ProbeError("AD-3 target configuration missing") from exc
    if ad not in ads:
        raise ProbeError("configured AD-3 is absent")
    primary_id = variables["KATCHA_OCI_PRIMARY_INSTANCE_ID"]
    primary = oci.read("compute", "instance", "get", "--instance-id", primary_id)
    if (
        not isinstance(primary, dict)
        or primary.get("lifecycle-state") != "RUNNING"
        or primary.get("shape") != "VM.Standard.A1.Flex"
    ):
        raise ProbeError("production primary is not verified running")
    seen_instances: set[str] = set()
    micros = 0
    for compartment in compartments:
        rows = _rows(
            oci.read("compute", "instance", "list", "--compartment-id", compartment, "--all"),
            "instance",
        )
        for row in rows:
            instance_id = row.get("id")
            if not isinstance(instance_id, str) or instance_id in seen_instances:
                raise ProbeError("instance inventory incomplete or duplicated")
            seen_instances.add(instance_id)
            if _active(row) and row.get("shape") == OCI_SHAPE:
                micros += 1
            if (
                for_new_launch
                and _active(row)
                and (row.get("freeform-tags") or {}).get("KatchaIsolationProbe") == "true"
            ):
                raise ProbeError("an isolated probe already exists")
    if primary_id not in seen_instances or (for_new_launch and micros >= FREE_MICROS):
        raise ProbeError("primary missing or no free Micro slot")
    seen_volumes: set[str] = set()
    storage = 0.0
    for compartment in compartments:
        for domain in ads:
            for kind in ("boot-volume", "volume"):
                rows = _rows(
                    oci.read(
                        "bv",
                        kind,
                        "list",
                        "--compartment-id",
                        compartment,
                        "--availability-domain",
                        domain,
                        "--all",
                    ),
                    kind,
                )
                for row in rows:
                    volume_id = row.get("id")
                    if not isinstance(volume_id, str):
                        raise ProbeError("volume inventory has no identifier")
                    if volume_id in seen_volumes:
                        continue
                    seen_volumes.add(volume_id)
                    if _active(row):
                        size = row.get("size-in-gbs")
                        if (
                            isinstance(size, bool)
                            or not isinstance(size, (int, float))
                            or size <= 0
                        ):
                            raise ProbeError("volume inventory has invalid size")
                        storage += size
    if for_new_launch and storage + BOOT_GB > FREE_STORAGE_GB:
        raise ProbeError("insufficient Always Free boot/block storage")
    shapes = _rows(
        oci.read(
            "compute",
            "shape",
            "list",
            "--compartment-id",
            production,
            "--availability-domain",
            ad,
            "--shape",
            OCI_SHAPE,
            "--all",
        ),
        "shape",
    )
    if not any(r.get("shape") == OCI_SHAPE for r in shapes):
        raise ProbeError("Micro shape not listed in AD-3")
    images = _rows(
        oci.read(
            "compute",
            "image",
            "list",
            "--compartment-id",
            production,
            "--shape",
            OCI_SHAPE,
            "--operating-system",
            "Oracle Linux",
            "--all",
        ),
        "image",
    )
    matching = [
        r
        for r in images
        if r.get("display-name") == IMAGE_NAME and r.get("lifecycle-state") == "AVAILABLE"
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("id"), str):
        raise ProbeError("pinned Oracle Linux Micro image unavailable")
    image = matching[0]
    size = image.get("size-in-mbs")
    minimum = image.get("minimum-provisioned-size-in-gbs")
    if (
        not isinstance(size, (int, float))
        or size <= 0
        or size > BOOT_GB * 1024
        or (minimum is not None and (not isinstance(minimum, (int, float)) or minimum > BOOT_GB))
    ):
        raise ProbeError("pinned image does not fit 50 GB boot volume")
    subnet = oci.read("network", "subnet", "get", "--subnet-id", subnet_id)
    primary_subnet = oci.read(
        "network", "subnet", "get", "--subnet-id", variables["KATCHA_OCI_SUBNET_ID"]
    )
    if (
        not isinstance(subnet, dict)
        or subnet.get("lifecycle-state") != "AVAILABLE"
        or subnet.get("prohibit-public-ip-on-vnic") is not True
        or subnet.get("compartment-id") != production
        or not _no_ingress(oci, subnet)
        or not _no_ingress(oci, primary_subnet)
    ):
        raise ProbeError("candidate or primary subnet isolation not verified")
    nsg_id = variables["KATCHA_OCI_ADMIN_NSG_ID"]
    rules = _rows(oci.read("network", "nsg", "rules", "list", "--nsg-id", nsg_id, "--all"), "NSG")
    bastion = oci.read(
        "bastion", "bastion", "get", "--bastion-id", variables["KATCHA_OCI_BASTION_ID"]
    )
    if not isinstance(bastion, dict) or bastion.get("lifecycle-state") != "ACTIVE":
        raise ProbeError("Bastion metadata unavailable or inactive")
    bastion_ip = bastion.get("private-endpoint-ip-address")
    if not isinstance(bastion_ip, str) or not _ssh_only_from_bastion(rules, bastion_ip):
        raise ProbeError("admin NSG allows more than Bastion SSH ingress")
    attachments = _rows(
        oci.read(
            "compute",
            "vnic-attachment",
            "list",
            "--compartment-id",
            production,
            "--instance-id",
            primary_id,
            "--all",
        ),
        "VNIC attachment",
    )
    active = [r for r in attachments if r.get("lifecycle-state") == "ATTACHED"]
    if len(active) != 1:
        raise ProbeError("primary VNIC inventory unexpected")
    primary_vnic = oci.read("network", "vnic", "get", "--vnic-id", active[0]["vnic-id"])
    if (
        not isinstance(primary_vnic, dict)
        or primary_vnic.get("subnet-id") != variables["KATCHA_OCI_SUBNET_ID"]
        or nsg_id not in (primary_vnic.get("nsg-ids") or [])
    ):
        raise ProbeError("admin NSG is not the primary's verified Bastion SSH group")
    public_key = variables["KATCHA_OCI_SSH_PUBLIC_KEY"]
    if not public_key.startswith("ssh-") or "\n" in public_key:
        raise ProbeError("configured SSH public key is invalid")
    return {
        "compartment_id": production,
        "primary_id": primary_id,
        "image_id": image["id"],
        "image_name": IMAGE_NAME,
        "subnet_id": subnet_id,
        "subnet_name": subnet.get("display-name"),
        "availability_domain": ad,
        "nsg_id": nsg_id,
        "ssh_public_key": public_key,
        "micro_slots_remaining": FREE_MICROS - micros,
        "storage_remaining_gb": FREE_STORAGE_GB - storage,
        "storage_after_launch_gb": FREE_STORAGE_GB - storage - BOOT_GB,
    }


def cleanup_plan() -> dict[str, str]:
    """Resolve stable identity fields without gating removal on launch eligibility."""
    production = variable("KATCHA_OCI_COMPARTMENT_ID")
    primary = variable("KATCHA_OCI_PRIMARY_INSTANCE_ID")
    nsg = variable("KATCHA_OCI_ADMIN_NSG_ID")
    try:
        targets = json.loads(variable("KATCHA_OCI_CROSS_AD_TARGETS_JSON"))
        target = next(t for t in targets if t["availability_domain"].endswith("AD-3"))
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise ProbeError("cleanup target metadata unavailable") from exc
    if not all(
        isinstance(value, str) and value.startswith("ocid1.")
        for value in (production, primary, nsg, target["subnet_id"])
    ):
        raise ProbeError("cleanup identity metadata invalid")
    return {
        "compartment_id": production,
        "primary_id": primary,
        "nsg_id": nsg,
        "subnet_id": target["subnet_id"],
        "availability_domain": target["availability_domain"],
    }


def launch_args(plan: dict[str, Any], key_path: Path, probe_id: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-f]{12}", probe_id):
        raise ProbeError("invalid probe identifier")
    tags = {"KatchaIsolationProbe": "true", "KatchaProbeId": probe_id, "KatchaProduction": "false"}
    return [
        "--compartment-id",
        plan["compartment_id"],
        "--availability-domain",
        plan["availability_domain"],
        "--display-name",
        f"katcha-identity-probe-{probe_id}",
        "--shape",
        OCI_SHAPE,
        "--image-id",
        plan["image_id"],
        "--subnet-id",
        plan["subnet_id"],
        "--nsg-ids",
        json.dumps([plan["nsg_id"]]),
        "--assign-public-ip",
        "false",
        "--boot-volume-size-in-gbs",
        str(BOOT_GB),
        "--ssh-authorized-keys-file",
        str(key_path),
        "--freeform-tags",
        json.dumps(tags, separators=(",", ":")),
        "--wait-for-state",
        "RUNNING",
        "--max-wait-seconds",
        "1200",
    ]


def verify(
    oci: Oci,
    plan: dict[str, Any],
    instance_id: str,
    *,
    for_cleanup: bool = False,
    allow_candidate_tag: bool = False,
) -> dict[str, Any]:
    instance = oci.read("compute", "instance", "get", "--instance-id", instance_id)
    if not isinstance(instance, dict):
        raise ProbeError("candidate instance metadata unavailable")
    tags = instance.get("freeform-tags") or {}
    defined = instance.get("defined-tags") or {}
    accepted_states = {"RUNNING", "STOPPED"} if for_cleanup else {"RUNNING"}
    candidate_tag = (defined.get("KatchaRecovery") or {}).get("Candidate") == "true"
    if (
        instance.get("lifecycle-state") not in accepted_states
        or instance.get("shape") != OCI_SHAPE
        or instance.get("availability-domain") != plan["availability_domain"]
        or tags.get("KatchaIsolationProbe") != "true"
        or tags.get("KatchaProduction") != "false"
        or (not for_cleanup and not allow_candidate_tag and candidate_tag)
        or instance_id == plan["primary_id"]
    ):
        raise ProbeError("candidate does not satisfy isolated instance contract")
    metadata = instance.get("metadata") or {}
    if "user_data" in metadata or "user-data" in metadata:
        raise ProbeError("candidate unexpectedly received cloud-init user data")
    attachments = _rows(
        oci.read(
            "compute",
            "volume-attachment",
            "list",
            "--compartment-id",
            plan["compartment_id"],
            "--instance-id",
            instance_id,
            "--all",
        ),
        "volume attachment",
    )
    if any(_active(row) for row in attachments):
        raise ProbeError("candidate unexpectedly has a data volume")
    boot_attachments = _rows(
        oci.read(
            "compute",
            "boot-volume-attachment",
            "list",
            "--compartment-id",
            plan["compartment_id"],
            "--instance-id",
            instance_id,
            "--all",
        ),
        "boot volume attachment",
    )
    active_boot = [row for row in boot_attachments if _active(row)]
    if len(active_boot) != 1 or not isinstance(active_boot[0].get("boot-volume-id"), str):
        raise ProbeError("candidate must have exactly one boot volume")
    boot = oci.read(
        "bv", "boot-volume", "get", "--boot-volume-id", active_boot[0]["boot-volume-id"]
    )
    if not isinstance(boot, dict) or boot.get("size-in-gbs") != BOOT_GB:
        raise ProbeError("candidate boot volume is not 50 GB")
    vnics = _rows(
        oci.read(
            "compute",
            "vnic-attachment",
            "list",
            "--compartment-id",
            plan["compartment_id"],
            "--instance-id",
            instance_id,
            "--all",
        ),
        "VNIC attachment",
    )
    active_vnics = [r for r in vnics if r.get("lifecycle-state") == "ATTACHED"]
    if len(active_vnics) != 1:
        raise ProbeError("candidate must have one attached VNIC")
    vnic = oci.read("network", "vnic", "get", "--vnic-id", active_vnics[0]["vnic-id"])
    if (
        not isinstance(vnic, dict)
        or vnic.get("public-ip")
        or vnic.get("subnet-id") != plan["subnet_id"]
        or vnic.get("nsg-ids") != [plan["nsg_id"]]
    ):
        raise ProbeError("candidate VNIC is outside the private Bastion-only network")
    primary_running = False
    if not for_cleanup:
        primary = oci.read("compute", "instance", "get", "--instance-id", plan["primary_id"])
        primary_running = isinstance(primary, dict) and primary.get("lifecycle-state") == "RUNNING"
        if not primary_running:
            raise ProbeError("production primary is no longer RUNNING")
    return {
        "status": "ISOLATED_INSTANCE_VERIFIED",
        "instance_id": instance_id,
        "primary_running": primary_running,
        "public_ip": False,
        "data_volumes": 0,
        "production_bootstrap": False,
        "vault_candidate_tag": candidate_tag,
        "recovery_enabled": False,
    }


def promote_identity_only_candidate(
    oci: Oci,
    plan: dict[str, Any],
    instance_id: str,
) -> dict[str, Any]:
    """Scoped candidate defined-tag transition, never a production bootstrap."""
    verify(oci, plan, instance_id)
    instance = oci.read("compute", "instance", "get", "--instance-id", instance_id)
    tags = instance.get("freeform-tags") or {}
    probe_id = tags.get("KatchaProbeId", "")
    if (
        not isinstance(probe_id, str)
        or not re.fullmatch(r"[0-9a-f]{12}", probe_id)
        or instance.get("display-name") != "katcha-identity-probe-" + probe_id
    ):
        raise ProbeError("identity tag refused: exact isolation probe identity missing")
    original = instance.get("defined-tags") or {}
    if not isinstance(original, dict) or any(
        not isinstance(value, dict) for value in original.values()
    ):
        raise ProbeError("identity tag refused: invalid defined-tag metadata")
    recovery_tags = original.get("KatchaRecovery") or {}
    if not isinstance(recovery_tags, dict) or "Candidate" in recovery_tags:
        raise ProbeError("identity tag refused: candidate tag already defined")
    updated = {
        **original,
        "KatchaRecovery": {**recovery_tags, "Candidate": "true"},
    }
    oci.update_probe_defined_tags(instance_id, updated)
    verified = verify(oci, plan, instance_id, allow_candidate_tag=True)
    if verified["vault_candidate_tag"] is not True:
        raise ProbeError("identity tag result unverified; reconcile exact probe")
    return {
        "status": "IDENTITY_PROBE_CANDIDATE_TAG_VERIFIED",
        "instance_id": instance_id,
        "vault_candidate_tag": True,
        "primary_running": verified["primary_running"],
        "no_production_bootstrap": True,
        "secret_access_tested": False,
        "recovery_enabled": False,
    }


def revoke_candidate_tag(
    oci: Oci,
    plan: dict[str, Any],
    instance_id: str,
) -> dict[str, Any]:
    """Revoke tag while guest remains available for negative Vault tests."""
    verify(oci, plan, instance_id, for_cleanup=True)
    instance = oci.read("compute", "instance", "get", "--instance-id", instance_id)
    tags = instance.get("freeform-tags") or {}
    if (
        not re.fullmatch(r"[0-9a-f]{12}", tags.get("KatchaProbeId", ""))
        or instance.get("display-name")
        != "katcha-identity-probe-" + tags["KatchaProbeId"]
    ):
        raise ProbeError("refused tag revocation: exact probe identity missing")
    original = instance.get("defined-tags") or {}
    recovery_tags = original.get("KatchaRecovery") or {}
    if recovery_tags.get("Candidate") == "true":
        remaining = {
            key: value for key, value in recovery_tags.items()
            if key != "Candidate"
        }
        new_defined = {**original}
        if remaining:
            new_defined["KatchaRecovery"] = remaining
        else:
            new_defined.pop("KatchaRecovery", None)
        oci.update_probe_defined_tags(instance_id, new_defined)
        after_revoke = oci.read(
            "compute", "instance", "get", "--instance-id", instance_id
        )
        after_tags = (after_revoke.get("defined-tags") or {}).get("KatchaRecovery") or {}
        if after_tags.get("Candidate") == "true":
            raise ProbeError(
                "cleanup refused: instance-principal candidate tag revocation unverified"
            )


    return {
        "status": "CANDIDATE_TAG_REMOVED_DATA_PLANE_NOT_YET_VERIFIED",
        "instance_id": instance_id,
        "vault_effective_access_revoked": False,
    }


def cleanup(
    oci: Oci,
    plan: dict[str, Any],
    instance_id: str,
    *,
    emergency: bool = False,
    expected_boot_id: str | None = None,
) -> dict[str, Any]:
    # verify checks the probe tag, no data volumes, private network, and
    # 50 GB boot volume even if the candidate is stopped or primary is down.
    verify(oci, plan, instance_id, for_cleanup=True)
    instance = oci.read("compute", "instance", "get", "--instance-id", instance_id)
    tags = instance.get("freeform-tags") or {}
    if (
        not instance.get("display-name", "").startswith("katcha-identity-probe-")
        or not re.fullmatch(r"[0-9a-f]{12}", tags.get("KatchaProbeId", ""))
        or instance["display-name"] != "katcha-identity-probe-" + tags["KatchaProbeId"]
    ):
        raise ProbeError("cleanup refused: instance identity is not the exact probe")
    boots = _rows(
        oci.read(
            "compute",
            "boot-volume-attachment",
            "list",
            "--compartment-id",
            plan["compartment_id"],
            "--instance-id",
            instance_id,
            "--all",
        ),
        "boot volume attachment",
    )
    attached = [row for row in boots if _active(row)]
    if len(attached) != 1:
        raise ProbeError("cleanup refused: probe boot volume is ambiguous")
    boot_id = attached[0].get("boot-volume-id")
    if not isinstance(boot_id, str) or not boot_id.startswith("ocid1.bootvolume."):
        raise ProbeError("cleanup refused: probe boot volume ID invalid")
    if emergency:
        if (
            not expected_boot_id
            or boot_id != expected_boot_id
            or not boot_id.startswith("ocid1.bootvolume.")
        ):
            raise ProbeError("emergency cleanup refused: exact boot volume mismatch")
    elif (
        (instance.get("defined-tags") or {}).get("KatchaRecovery") or {}
    ).get("Candidate") == "true":
        raise ProbeError(
            "normal cleanup refused: revoke candidate tag and verify "
            "four post-revocation Vault denials before termination"
        )

    oci.terminate_test_instance(instance_id)
    for _ in range(24):
        rows = _rows(
            oci.read(
                "bv",
                "boot-volume",
                "list",
                "--compartment-id",
                plan["compartment_id"],
                "--availability-domain",
                plan["availability_domain"],
                "--all",
            ),
            "boot volume",
        )
        remaining = [row for row in rows if row.get("id") == boot_id and _active(row)]
        if not remaining:
            break
        time.sleep(5)
    else:
        raise ProbeError("probe terminated but its boot volume is still present")
    try:
        primary = oci.read("compute", "instance", "get", "--instance-id", plan["primary_id"])
        primary_running = isinstance(primary, dict) and primary.get("lifecycle-state") == "RUNNING"
    except ProbeError:
        primary_running = False
    return {
        "status": (
            "EMERGENCY_TEST_RESOURCES_REMOVED_IAM_UNVERIFIED"
            if emergency else "TEST_RESOURCES_REMOVED"
        ),
        "instance_id": instance_id,
        "production_api_readiness_verified": False,
        "original_disk_inventory_verified": False,
        "effective_iam_revocation_verified": False,
        "test_boot_volume_removed": True,
        "primary_running": primary_running,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=(
            "plan", "launch", "verify", "tag-candidate", "revoke-tag",
            "cleanup", "emergency-contain",
        )
    )
    parser.add_argument("--instance-id", help="required for verify or cleanup")
    parser.add_argument("--approve-launch-and-storage-exhaustion", action="store_true")
    parser.add_argument("--approve-cleanup-of-test-instance-and-boot-volume", action="store_true")
    parser.add_argument("--approve-isolated-candidate-iam-tag", action="store_true")
    parser.add_argument("--acknowledge-four-untagged-vault-denials", action="store_true")
    parser.add_argument("--approve-isolated-candidate-tag-revocation", action="store_true")
    parser.add_argument("--acknowledge-post-revocation-four-denials", action="store_true")
    parser.add_argument("--approve-emergency-probe-containment", action="store_true")
    parser.add_argument("--expected-boot-volume-id")
    options = parser.parse_args(argv)
    try:
        oci = Oci()
        plan = (
            cleanup_plan()
            if options.mode in {"cleanup", "emergency-contain", "revoke-tag"}
            else gather(oci, for_new_launch=options.mode in {"plan", "launch"})
        )
        if options.mode == "plan":
            print(
                json.dumps(
                    {
                        k: plan[k]
                        for k in (
                            "image_name",
                            "subnet_name",
                            "availability_domain",
                            "micro_slots_remaining",
                            "storage_remaining_gb",
                            "storage_after_launch_gb",
                        )
                    },
                    sort_keys=True,
                )
            )
            print(
                "PLAN_REVIEWABLE: one E2 Micro, 50 GB boot, no public IP, no data disk, "
                "no production bootstrap or Vault candidate tag; launch requires explicit approval"
            )
            return 0
        if options.mode == "verify":
            if not options.instance_id:
                raise ProbeError("verify requires --instance-id")
            print(json.dumps(verify(oci, plan, options.instance_id), sort_keys=True))
            return 0
        if options.mode == "tag-candidate":
            if (
                not options.instance_id
                or not options.approve_isolated_candidate_iam_tag
                or not options.acknowledge_four_untagged_vault_denials
            ):
                raise ProbeError(
                    "tag-candidate requires exact instance ID, separate IAM-tag "
                    "approval, and confirmed four negative Vault checks"
                )
            print(json.dumps(
                promote_identity_only_candidate(oci, plan, options.instance_id),
                sort_keys=True,
            ))
            return 0
        if options.mode == "revoke-tag":
            if (
                not options.instance_id
                or not options.approve_isolated_candidate_tag_revocation
            ):
                raise ProbeError(
                    "revoke-tag requires exact instance ID and pre-approved scope"
                )
            print(json.dumps(
                revoke_candidate_tag(oci, plan, options.instance_id),
                sort_keys=True,
            ))
            return 0
        if options.mode == "cleanup":
            if (
                not options.instance_id
                or not options.approve_cleanup_of_test_instance_and_boot_volume
                or not options.acknowledge_post_revocation_four_denials
            ):
                raise ProbeError(
                    "cleanup requires exact instance ID, pre-authorized deletion, "
                    "and witnessed four negative post-revocation Vault checks"
                )
            print(json.dumps(cleanup(oci, plan, options.instance_id), sort_keys=True))
            return 0
        if options.mode == "emergency-contain":
            if (
                not options.instance_id
                or not options.expected_boot_volume_id
                or not options.approve_emergency_probe_containment
            ):
                raise ProbeError(
                    "emergency containment requires exact instance ID, exact "
                    "boot-volume ID and advance containment authorization"
                )
            print(json.dumps(cleanup(
                oci, plan, options.instance_id, emergency=True,
                expected_boot_id=options.expected_boot_volume_id,
            ), sort_keys=True))
            return 0
        if not options.approve_launch_and_storage_exhaustion:
            raise ProbeError("launch requires explicit approval and storage-exhaustion flag")
        probe_id = uuid.uuid4().hex[:12]
        print("PROBE_ID: " + probe_id, flush=True)
        with tempfile.TemporaryDirectory(prefix="katcha-isolated-probe-") as directory:
            key_path = Path(directory) / "id.pub"
            key_path.write_text(plan["ssh_public_key"] + "\n", encoding="utf-8")
            key_path.chmod(0o600)
            instance = oci.launch(launch_args(plan, key_path, probe_id))
        instance_id = instance.get("id")
        if not isinstance(instance_id, str) or not instance_id.startswith("ocid1.instance."):
            raise ProbeError("launch outcome uncertain; reconcile by KatchaProbeId before retry")
        print(json.dumps(verify(oci, plan, instance_id), sort_keys=True))
        return 0
    except ProbeError as exc:
        print("PROBE_STOP: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
