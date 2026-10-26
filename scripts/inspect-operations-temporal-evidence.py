#!/usr/bin/env python3
"""Read-only operator inspection of bounded root-private Temporal failure receipts."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER = Path("/srv/katcha/operations/ledger.sqlite3")


def summarize(rows: list[sqlite3.Row], *, include_private_ids: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "evidence_id": row["fingerprint"][:16],
            "workflow_type": row["workflow_type"],
            "cause_code": row["cause_code"],
            "first_seen_at": datetime.fromtimestamp(row["first_seen"], UTC).isoformat(),
            "last_seen_at": datetime.fromtimestamp(row["last_seen"], UTC).isoformat(),
        }
        if include_private_ids:
            item["workflow_id"] = row["workflow_id"]
            item["run_id"] = row["run_id"]
        records.append(item)
    return records


def inspect(limit: int, *, include_private_ids: bool) -> list[dict[str, Any]]:
    if os.geteuid() != 0:
        raise PermissionError("Run on the production host as root; evidence is private.")
    if LEDGER.is_symlink() or not LEDGER.is_file():
        raise FileNotFoundError("The root-private operations ledger is unavailable.")
    metadata = LEDGER.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("The operations ledger has unsafe ownership or permissions.")
    with sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT fingerprint, workflow_id, run_id, workflow_type, cause_code, "
            "first_seen, last_seen FROM temporal_execution_evidence "
            "ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return summarize(rows, include_private_ids=include_private_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--private-ids", action="store_true",
        help="Explicitly display raw workflow/run IDs. Never paste this output into public issues.",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    try:
        records = inspect(args.limit, include_private_ids=args.private_ids)
    except (OSError, sqlite3.Error) as exc:
        # Avoid printing paths and database text on unexpected failures.
        print(json.dumps({"status": "UNAVAILABLE", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps({"status": "OK", "count": len(records), "records": records}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
