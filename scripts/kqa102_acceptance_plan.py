#!/usr/bin/env python3
"""Prepare a bounded KQA-102 plan offline; this tool cannot execute provider work."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 1_048_576
EVIDENCE_CHECKLIST = (
    "exact channel/project/revision, script digest and release SHA",
    "separate operator authorization bound to this plan digest and traffic/spend caps",
    "KQA-101 full-path deterministic acceptance evidence",
    "current provider capability and enforced per-request/total ceilings",
    "source permission, expiry, attribution and current rights assessment/version",
    "durable search/acquisition receipts, reservations and unknown-outcome reconciliation",
    "actual media SHA-256, bytes, probe results and canonical Media Bin identities",
    "measured source timestamps/timebase and sparse observation limitations",
    "requirement coverage, reference-only entries and explicit unresolved gaps",
    "portable export checksums, references and retained evidence",
    "locked operator choices, storyboard binding and final compile/render rights rechecks",
    "crash/restart/replay and cross-channel denial evidence",
)


def _fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(expected))}")
    return value


def _integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{label} must be a nonzero canonical UUID")
    return value


def _label(value: Any, label: str) -> str:
    # References are durable record labels, not URLs, tokens or credential payloads.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value):
        raise ValueError(f"{label} must be a bounded record label without query strings or secrets")
    if "://" in value:
        raise ValueError(f"{label} must be a record label, not a URL")
    return value


def _digest(value: Any, length: int, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ValueError(f"{label} must be an exact lowercase digest")
    return value


def _references(value: Any, label: str) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError(f"{label} requires 1 to 32 durable evidence record labels")
    for reference in value:
        _label(reference, label)
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicate references")


def validate_spec(raw: Any) -> dict[str, Any]:
    spec = _fields(
        raw,
        {
            "format_version",
            "channel_id",
            "project_id",
            "revision",
            "script_sha256",
            "release_sha",
            "operator",
            "authorization_ref",
            "sources",
            "providers",
        },
        "plan",
    )
    _integer(spec["format_version"], 1, 1, "format_version")
    channel = _identity(spec["channel_id"], "channel_id")
    project = _identity(spec["project_id"], "project_id")
    _integer(spec["revision"], 1, 2_147_483_647, "revision")
    _digest(spec["script_sha256"], 64, "script_sha256")
    _digest(spec["release_sha"], 40, "release_sha")
    _label(spec["operator"], "operator")
    # This reference is a planning declaration; it is never treated as live consent.
    if spec["authorization_ref"] is not None:
        _label(spec["authorization_ref"], "authorization_ref")
    if not isinstance(spec["sources"], list) or not 1 <= len(spec["sources"]) <= 100:
        raise ValueError("sources requires 1 to 100 explicitly scoped sources")
    source_ids = []
    for source in spec["sources"]:
        _fields(
            source,
            {
                "source_id",
                "channel_id",
                "project_id",
                "source_class",
                "sha256",
                "permission_evidence_refs",
                "inspection_authorized",
                "acquisition_authorized",
                "production_authorized",
                "permission_expires_at",
            },
            "source",
        )
        source_ids.append(_label(source["source_id"], "source_id"))
        if source["channel_id"] != channel or source["project_id"] != project:
            raise ValueError("source belongs to another channel/project")
        if not isinstance(source["source_class"], str) or source["source_class"] not in {
            "owned",
            "licensed",
            "reference_only",
        }:
            raise ValueError("unsupported source_class")
        if source["sha256"] is not None:
            _digest(source["sha256"], 64, "source sha256")
        _references(source["permission_evidence_refs"], "permission_evidence_refs")
        for field in ("inspection_authorized", "acquisition_authorized", "production_authorized"):
            if type(source[field]) is not bool:
                raise ValueError(f"{field} must be a boolean")
        if source["source_class"] == "reference_only" and (
            source["acquisition_authorized"] or source["production_authorized"]
        ):
            raise ValueError("reference-only sources cannot authorize acquisition or production")
        if source["production_authorized"] and not source["inspection_authorized"]:
            raise ValueError("production use requires inspection permission")
        expiry = source["permission_expires_at"]
        if expiry is not None:
            if not isinstance(expiry, str):
                raise ValueError("permission expiry must be an ISO timestamp with timezone")
            try:
                timestamp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("invalid permission expiry") from exc
            if timestamp.tzinfo is None:
                raise ValueError("permission expiry requires timezone")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("duplicate source_id")
    if not isinstance(spec["providers"], list) or len(spec["providers"]) > 16:
        raise ValueError("providers must be a bounded list with at most 16 entries")
    provider_ids = []
    for provider in spec["providers"]:
        _fields(
            provider,
            {"key", "version", "max_calls", "max_results", "max_cost_microdollars"},
            "provider",
        )
        provider_ids.append(
            (
                _label(provider["key"], "provider key"),
                _label(provider["version"], "provider version"),
            )
        )
        _integer(provider["max_calls"], 1, 1000, "max_calls")
        _integer(provider["max_results"], 1, 10000, "max_results")
        _integer(provider["max_cost_microdollars"], 0, 1_000_000_000, "max_cost_microdollars")
    if len(set(provider_ids)) != len(provider_ids):
        raise ValueError("duplicate provider identity")
    return spec


def prepare_plan(raw: Any) -> dict[str, Any]:
    # Snapshot caller input so no returned plan can mutate its validated declaration.
    spec = validate_spec(json.loads(json.dumps(raw, allow_nan=False)))
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return {
        "format_version": 1,
        "ticket": "KQA-102",
        "status": "offline_prepared",
        "plan_sha256": hashlib.sha256(encoded).hexdigest(),
        "scope_and_declarations": spec,
        "live_execution_enabled": False,
        "provider_calls_executed": 0,
        "production_acceptance": False,
        "declarations_verified": False,
        "required_before_live": list(EVIDENCE_CHECKLIST),
        "recovery_rule": (
            "Never retry a running/unknown provider outcome or release its reservation; "
            "restore and reconcile evidence first."
        ),
        "next_action": "Complete KQA-101 and obtain separate operator authorization for this exact "
        "plan. Recheck live source rights and enforced provider ceilings before work.",
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Local plan specification JSON")
    parser.add_argument("--output", type=Path, required=True, help="New offline report file")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("plan input exceeds 1 MiB")
        raw = json.loads(data, object_pairs_hook=_unique_object)
        plan = prepare_plan(raw)
        # Never replace existing operator input/evidence or overwrite a prior plan.
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Plan preparation failed: {exc}\n")
    print("Offline plan prepared. No provider calls or production acceptance occurred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
