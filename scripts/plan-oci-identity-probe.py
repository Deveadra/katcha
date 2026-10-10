#!/usr/bin/env python3
"""Evaluate supplied, non-secret metadata for ONE isolated E2 identity probe.

Offline only: no credentials, subprocesses, networking, resource operations, or
approval switch. A reviewable plan is never authorization to launch or read Vault.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

SHAPE = "VM.Standard.E2.1.Micro"
SWITCHES = (
    "KATCHA_OCI_RECOVERY_CONFIGURED",
    "KATCHA_EXTERNAL_COMPUTE_ENABLED",
    "KATCHA_OCI_PAID_FALLBACK_ENABLED",
)
CHECKS = (
    "primary_healthy",
    "inventory_complete",
    "home_region_verified",
    "account_free_tier_verified",
    "billing_reviewed",
    "quota_verified",
    "image_shape_compatible",
    "private_network_egress_verified",
    "iam_metadata_verified",
)


def evaluate(evidence: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    blockers: list[str] = []
    for key in CHECKS:
        if evidence.get(key) is not True:
            blockers.append(key)
    for key in SWITCHES:
        if evidence.get(key) is not False:
            blockers.append(key)
    try:
        stamp = datetime.fromisoformat(evidence["observed_at"])
        age = now - stamp
        if not timedelta(0) <= age <= timedelta(hours=1):
            blockers.append("fresh_metadata_required")
    except (KeyError, TypeError, ValueError):
        blockers.append("fresh_metadata_required")

    def number(key: str, *, integer: bool = False) -> float | None:
        value = evidence.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or (integer and int(value) != value)):
            blockers.append(key)
            return None
        return float(value)

    slots = number("micro_slots_remaining", integer=True)
    available = number("storage_remaining_gb")
    image_min = number("image_minimum_boot_gb")
    boot = number("planned_boot_gb", integer=True)
    if slots is not None and slots < 1:
        blockers.append("no_micro_slot")
    if image_min is not None and image_min <= 0:
        blockers.append("image_minimum_boot_gb")
    if boot is not None and (boot < 50 or (image_min is not None and boot < image_min)):
        blockers.append("boot_below_minimum")
    if boot is not None and available is not None and boot > available:
        blockers.append("insufficient_storage")
    if evidence.get("shape") != SHAPE:
        blockers.append("shape")
    ad = evidence.get("availability_domain")
    candidates = evidence.get("shape_availability_domains")
    if (not isinstance(ad, str) or not ad or not isinstance(candidates, list)
            or not all(isinstance(item, str) and item for item in candidates)
            or ad not in candidates):
        blockers.append("availability_domain")
    # This plan has no data disk, public IP, production bootstrap, or volume reuse.
    # Those would require a different plan and explicit authorization.
    for key in ("attach_production_volume", "assign_public_ip", "production_bootstrap"):
        if evidence.get(key) is not False:
            blockers.append(key)
    extra = evidence.get("additional_data_gb")
    if isinstance(extra, bool) or not isinstance(extra, (int, float)) or extra != 0:
        blockers.append("additional_data_gb")
    return {
        "status": "BLOCKED" if blockers else "PLAN_REVIEWABLE",
        "blockers": sorted(set(blockers)),
        "planned_instances": 1,
        "planned_data_gb": 0,
        "storage_after_probe_gb": (
            available - boot if available is not None and boot is not None else None
        ),
        "launch_authorized": False,
        "secret_access_authorized": False,
        "recovery_acceptance_complete": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Non-secret metadata JSON file")
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise ValueError("expected object")
        result = evaluate(evidence)
    except (OSError, ValueError, TypeError):
        # Never echo arbitrary file contents or exception text.
        print('{"status":"INVALID_EVIDENCE"}')
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "PLAN_REVIEWABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
