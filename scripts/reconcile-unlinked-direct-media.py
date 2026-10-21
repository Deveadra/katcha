#!/usr/bin/env python3
"""Review source-owned R2 raw media; apply only with an exact evidence token."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from temporalio.client import Client

from katcha.config import get_settings
from katcha.ops.linked_ingest_reconciliation import RecoveryApprovalError
from katcha.ops.unlinked_direct_reconciliation import (
    finalize_unlinked_direct_recovery,
    plan_unlinked_direct_recovery,
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
        # No raw exception material; inability to verify Temporal denies approval.
        pass
    if args.apply:
        try:
            result = await finalize_unlinked_direct_recovery(
                args.source_id, approved_fingerprint=args.approve_fingerprint,
                temporal_client=client,
            )
        except RecoveryApprovalError as exc:
            print(json.dumps({"applied": False, "error": str(exc)}, sort_keys=True))
            return 3
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    result = await plan_unlinked_direct_recovery(
        args.source_id, temporal_client=client,
    )
    # Proposal may contain lengthy sanitized source-backed media metadata. Only
    # emit a compact operator view, not any raw source URL or full claim.
    result.pop("source_snapshot", None)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["eligible"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover an unlinked direct-media raw object with durable source provenance"
    )
    parser.add_argument("source_id", type=uuid.UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-fingerprint")
    args = parser.parse_args()
    if args.apply != bool(args.approve_fingerprint):
        parser.error("--apply requires --approve-fingerprint, and vice versa")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
