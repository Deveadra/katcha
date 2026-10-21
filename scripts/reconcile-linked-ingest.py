#!/usr/bin/env python3
"""Operator-reviewed linked Clip recovery, read-only unless --apply is explicit."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from temporalio.client import Client

from katcha.config import get_settings
from katcha.ops.linked_ingest_reconciliation import (
    RecoveryApprovalError,
    finalize_linked_clip_recovery,
    plan_linked_clip_recovery,
)


async def _run(args: argparse.Namespace) -> int:
    client = None
    try:
        settings = get_settings()
        client = await asyncio.wait_for(
            Client.connect(settings.temporal_host, namespace=settings.temporal_namespace),
            timeout=8.0,
        )
    except Exception:
        # The plan will report unverified Temporal state; never infer absence.
        pass
    if args.apply:
        try:
            result = await finalize_linked_clip_recovery(
                args.source_id,
                approved_fingerprint=args.approve_fingerprint,
                temporal_client=client,
            )
        except RecoveryApprovalError as exc:
            print(json.dumps({"applied": False, "error": str(exc)}, sort_keys=True))
            return 3
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    result = await plan_linked_clip_recovery(args.source_id, temporal_client=client)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["eligible"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evidence-fenced finalization of a held ingest with an existing Clip"
    )
    parser.add_argument("source_id", type=uuid.UUID)
    parser.add_argument(
        "--apply", action="store_true",
        help="Commit the reviewed linked-Clip finalization, never a workflow retry",
    )
    parser.add_argument(
        "--approve-fingerprint",
        help="Exact 64-character approval fingerprint from a prior eligible read-only plan",
    )
    args = parser.parse_args()
    if args.apply != bool(args.approve_fingerprint):
        parser.error("--apply requires --approve-fingerprint, and vice versa")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
