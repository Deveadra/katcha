#!/usr/bin/env python3
"""Read-only inspection of a held ingest across Temporal, DB and R2.

No retry, workflow reset, source-state mutation, staging deletion or upload.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from temporalio.client import Client

from katcha.config import get_settings
from katcha.ops.ingest_recovery_evidence import inspect_held_ingest


async def _run(args: argparse.Namespace) -> int:
    client = None
    try:
        settings = get_settings()
        client = await asyncio.wait_for(
            Client.connect(
                settings.temporal_host,
                namespace=settings.temporal_namespace,
            ),
            timeout=8.0,
        )
    except Exception:
        # No raw connection exception: transport strings may include secrets.
        # The structured report will show Temporal visibility as unavailable.
        pass
    result = await inspect_held_ingest(
        args.source_id,
        temporal_client=client,
        max_records=args.max_records,
        verify_sha256=args.verify_sha256,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result["assessment"] == "source_not_found" else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Temporal/SourceItem/Clip/R2 recovery evidence"
    )
    parser.add_argument("source_id", type=uuid.UUID, help="Exact SourceItem UUID")
    parser.add_argument(
        "--max-records", type=int, default=200,
        help="Maximum R2 objects and incomplete uploads inventoried each (1-2000)",
    )
    parser.add_argument(
        "--verify-sha256", action="store_true",
        help="Opt into reading up to 4 GiB of linked Clip object data from R2",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
