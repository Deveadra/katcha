#!/usr/bin/env python3
"""Read-only OCI home-region Always Free candidate capacity inventory.

This does NOT establish pricing eligibility or launch any instance.
Requires the separate KATCHA_VAULT_ADMIN security-token session and gh auth.
Never retrieves secret contents, never writes OCI resources, and never
modifies Katcha recovery switches, leader, primary, or persistent volumes.
"""
from __future__ import annotations

import argparse
import configparser
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

A1_SHAPE = "VM.Standard.A1.Flex"
MICRO_SHAPE = "VM.Standard.E2.1.Micro"
A1_FREE_OCPUS = 2.0
A1_FREE_MEMORY_GB = 12.0
MICRO_FREE_INSTANCES = 2
FREE_BOOT_AND_BLOCK_GB = 200.0
MIN_PROBE_BOOT_GB = 50.0


def _load_audit():
    path = Path(__file__).with_name("audit-oci-recovery-readiness.py")
    spec = importlib.util.spec_from_file_location("katcha_oci_readonly_audit", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("read-only IAM audit script missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_admin_session(audit: Any, base: list[str]) -> None:
    """Classify expired administrator sessions before scanning OCI."""
    try:
        audit.read_command(base + ["session", "validate"])
    except audit.AuditError as exc:
        raise ValueError(
            "OCI_ADMIN_SESSION_UNAVAILABLE: the security-token session "
            "has expired or cannot be validated. This is unrelated to the "
            "Bastion SSH tunnel. Reauthenticate KATCHA_VAULT_ADMIN, then retry."
        ) from exc


def read_oci_metadata(audit: Any, base: list[str], command: list[str]) -> dict:
    """Fail closed with a safe request category, never raw OCI credentials."""
    if not (
        command[-3:] == ["--all", "--output", "json"]
        or command[-2:] == ["--output", "json"]
    ):
        raise ValueError("OCI inventory command must request JSON metadata")
    stage = " ".join(command[:3])
    try:
        return audit.metadata(base + command, empty_list_ok=True)
    except audit.AuditError as exc:
        raise ValueError(
            "OCI_READ_FAILED at " + stage
            + ": this read-only request failed. Confirm the administrator "
            "security-token session is still valid, then check OCI read "
            "permissions for this resource type. No resources were modified."
        ) from exc


def _data(value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("data")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("OCI returned unexpected list metadata")
    return rows


def _active(row: dict[str, Any]) -> bool:
    # Until termination completes, reserved compute/storage may still occupy
    # allowance. Do not tell the operator an in-flight deletion freed quota.
    return str(row.get("lifecycle-state", "")).upper() not in {
        "TERMINATED", "DELETED",
    }


def _number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"unrecognized {label} metadata; refusing free-capacity inference")
    return float(value)


@dataclass(frozen=True)
class Inventory:
    primary_running: bool
    a1_ocpus: float
    a1_memory_gb: float
    micros: int
    boot_and_block_gb: float
    micro_availability_domains: tuple[str, ...]
    number_of_compartments: int
    number_of_instances: int
    number_of_storage_volumes: int

    @property
    def micro_slots_left(self) -> int:
        return max(0, MICRO_FREE_INSTANCES - self.micros)

    @property
    def free_storage_gb(self) -> float:
        return max(0.0, FREE_BOOT_AND_BLOCK_GB - self.boot_and_block_gb)

    @property
    def a1_headroom_ocpus(self) -> float:
        return max(0.0, A1_FREE_OCPUS - self.a1_ocpus)

    @property
    def a1_headroom_memory_gb(self) -> float:
        return max(0.0, A1_FREE_MEMORY_GB - self.a1_memory_gb)

    @property
    def micro_probe_candidate(self) -> bool:
        return (
            self.primary_running
            and self.micro_slots_left > 0
            and self.free_storage_gb >= MIN_PROBE_BOOT_GB
            and bool(self.micro_availability_domains)
        )


def inventory(fetch, tenancy: str, region: str, production: str, primary: str) -> Inventory:
    """Inspect all compartments and ADs; never silently undercount missing data."""
    if not (tenancy.startswith("ocid1.tenancy.")
            and production.startswith("ocid1.compartment.")
            and primary.startswith("ocid1.instance.")):
        raise ValueError("tenancy, production compartment, and primary IDs are required")

    regions = _data(fetch([
        "iam", "region-subscription", "list", "--tenancy-id", tenancy, "--output", "json",
    ]))
    home = [r for r in regions if r.get("is-home-region") is True]
    if len(home) != 1 or home[0].get("region-name") != region:
        raise ValueError("selected OCI region is not verified as the tenancy home region")

    subdivisions = _data(fetch([
        "iam", "compartment", "list", "--compartment-id", tenancy,
        "--compartment-id-in-subtree", "true", "--access-level", "ANY",
        "--all", "--output", "json",
    ]))
    compartments = [tenancy]
    for row in subdivisions:
        if row.get("lifecycle-state") not in ("ACTIVE",):
            continue
        cid = row.get("id")
        if not isinstance(cid, str) or not cid.startswith("ocid1.compartment."):
            raise ValueError("invalid compartment inventory metadata")
        if cid not in compartments:
            compartments.append(cid)
    if production not in compartments:
        raise ValueError("production compartment missing from tenancy inventory")

    ads = _data(fetch([
        "iam", "availability-domain", "list", "--compartment-id", tenancy,
        "--all", "--output", "json",
    ]))
    domains = [r.get("name") for r in ads]
    if not domains or any(not isinstance(name, str) or not name for name in domains):
        raise ValueError("could not enumerate all availability domains")

    a1_ocpus = 0.0
    a1_memory = 0.0
    micros = 0
    seen_instances: set[str] = set()
    primary_running = False
    print(f"READ_ONLY_INVENTORY: {len(compartments)} compartments, {len(domains)} ADs",
          flush=True)
    for compartment in compartments:
        instances = _data(fetch([
            "compute", "instance", "list", "--compartment-id", compartment,
            "--all", "--output", "json",
        ]))
        for inst in instances:
            instance_id = inst.get("id")
            if not isinstance(instance_id, str) or not instance_id.startswith("ocid1.instance."):
                raise ValueError("OCI instance lacks an ID")
            if instance_id in seen_instances:
                raise ValueError("duplicate instance in inventory")
            seen_instances.add(instance_id)
            state = str(inst.get("lifecycle-state", "")).upper()
            if instance_id == primary:
                primary_running = state == "RUNNING" and compartment == production
            if not _active(inst):
                continue
            shape = inst.get("shape")
            if not isinstance(shape, str) or not shape:
                raise ValueError("OCI instance lacks a shape")
            if shape == A1_SHAPE:
                details = inst.get("shape-config")
                if not isinstance(details, dict):
                    # ListInstance sometimes omits shape config. Read only this
                    # instance's metadata rather than counting it as zero.
                    detail = fetch([
                        "compute", "instance", "get", "--instance-id",
                        instance_id, "--output", "json",
                    ]).get("data")
                    details = detail.get("shape-config") if isinstance(detail, dict) else None
                if not isinstance(details, dict):
                    raise ValueError("A1 instance missing CPU/RAM shape config")
                a1_ocpus += _number(details.get("ocpus"), label="A1 OCPU")
                a1_memory += _number(
                    details.get("memory-in-gbs"), label="A1 memory"
                )
            if shape == MICRO_SHAPE:
                micros += 1

    if not primary_running:
        raise ValueError("expected live production primary not verified as RUNNING")

    seen_volumes: set[str] = set()
    storage = 0.0
    for compartment in compartments:
        for ad in domains:
            for command in (("bv", "boot-volume", "list"), ("bv", "volume", "list")):
                records = _data(fetch([
                    *command, "--compartment-id", compartment,
                    "--availability-domain", ad, "--all", "--output", "json",
                ]))
                for vol in records:
                    vid = vol.get("id")
                    if not isinstance(vid, str) or not vid:
                        raise ValueError("OCI volume lacks an ID")
                    if vid in seen_volumes:
                        continue
                    seen_volumes.add(vid)
                    if _active(vol):
                        storage += _number(vol.get("size-in-gbs"), label="volume size")
    supported_domains = []
    for ad in domains:
        shapes = _data(fetch([
            "compute", "shape", "list", "--compartment-id", production,
            "--availability-domain", ad, "--shape", MICRO_SHAPE,
            "--all", "--output", "json",
        ]))
        if any(shape.get("shape") == MICRO_SHAPE for shape in shapes):
            supported_domains.append(ad)

    return Inventory(
        primary_running=primary_running, a1_ocpus=a1_ocpus,
        a1_memory_gb=a1_memory, micros=micros,
        boot_and_block_gb=storage,
        micro_availability_domains=tuple(supported_domains),
        number_of_compartments=len(compartments),
        number_of_instances=len(seen_instances),
        number_of_storage_volumes=len(seen_volumes),
    )


def report(result: Inventory) -> None:
    print("\n=== OCI HOME-REGION FREE-TIER INVENTORY ===")
    print("production_primary_running:", "PASS" if result.primary_running else "STOP")
    print("tenancy_compartments_scanned:", result.number_of_compartments)
    print("instances_enumerated:", result.number_of_instances)
    print("boot_block_volumes_enumerated:", result.number_of_storage_volumes)
    print(f"A1_ocpus_allocated: {result.a1_ocpus:g} / {A1_FREE_OCPUS:g}")
    print(f"A1_memory_allocated_gb: {result.a1_memory_gb:g} / {A1_FREE_MEMORY_GB:g}")
    print(f"A1_remaining_ocpus: {result.a1_headroom_ocpus:g}")
    print(f"A1_remaining_memory_gb: {result.a1_headroom_memory_gb:g}")
    print(f"E2_micro_instances_allocated: {result.micros} / {MICRO_FREE_INSTANCES}")
    print(f"E2_micro_remaining_slots: {result.micro_slots_left}")
    print(f"boot_plus_block_storage_allocated_gb: {result.boot_and_block_gb:g} / "
          f"{FREE_BOOT_AND_BLOCK_GB:g}")
    print(f"boot_plus_block_storage_remaining_gb: {result.free_storage_gb:g}")
    print("E2_micro_shape_ad_candidates:",
          ", ".join(result.micro_availability_domains) or "NONE")
    print("E2_micro_identity_probe_eligibility:",
          "POSSIBLE_NOT_BILLING_GUARANTEED" if result.micro_probe_candidate else "BLOCKED")
    if result.a1_headroom_ocpus < 1 or result.a1_headroom_memory_gb < 1:
        print("parallel_A1_probe: BLOCKED_BY_ALWAYS_FREE_ALLOWANCE")
    else:
        print("parallel_A1_probe: UNVERIFIED_DO_NOT_LAUNCH")
    print("INSTANCE_LAUNCH_PERFORMED: NO")
    print("WARNING: Shape listing is not host capacity and free allowance is not "
          "a cost guarantee. Confirm account tier, quotas, networking, current "
          "billing, and image compatibility before separately authorizing "
          "an E2 Micro probe.")
    print("READ_ONLY_CAPACITY_PREFLIGHT_COMPLETE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-profile", default="KATCHA_VAULT_ADMIN")
    args = parser.parse_args()
    audit = _load_audit()
    variables = audit.github_vars()
    print("=== SAFETY GATES ===")
    for key in ("KATCHA_OCI_RECOVERY_CONFIGURED",
                "KATCHA_EXTERNAL_COMPUTE_ENABLED",
                "KATCHA_OCI_PAID_FALLBACK_ENABLED"):
        state = variables.get(key, "").lower()
        print(key + ": " + ("SAFE_FALSE" if state == "false" else "STOP"))
        if state != "false":
            raise ValueError("Recovery safety gate is not disabled")

    cfg = configparser.ConfigParser(interpolation=None)
    file = Path.home() / ".oci/config"
    if not cfg.read(file) or args.admin_profile not in cfg:
        raise ValueError("OCI administrator security-token profile unavailable")
    tenancy = cfg[args.admin_profile].get("tenancy", "")
    region = variables.get("OCI_REGION") or "us-ashburn-1"
    cli = Path.home() / ".cache/katcha/oci-cli-3.94.1/bin/oci"
    if not cli.is_file():
        raise ValueError("isolated OCI CLI executable missing")

    base = [
        str(cli), "--config-file", str(file), "--profile", args.admin_profile,
        "--auth", "security_token", "--region", region,
    ]
    validate_admin_session(audit, base)

    def read_only_fetch(command):
        # Keep OCI metadata failure stage visible but never echo command IDs,
        # provider stderr, tokens, Vault contents, or the environment.
        return read_oci_metadata(audit, base, command)

    result = inventory(
        read_only_fetch, tenancy, region,
        variables.get("KATCHA_OCI_COMPARTMENT_ID", ""),
        variables.get("KATCHA_OCI_PRIMARY_INSTANCE_ID", ""),
    )
    report(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as exc:
        print("CAPACITY_PREFLIGHT_STOP: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        # Unexpected failures still remain non-secret and non-mutating.
        print(
            "CAPACITY_PREFLIGHT_STOP: unexpected local preflight error ("
            + type(exc).__name__ + "); no OCI resources changed",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
