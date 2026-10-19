#!/usr/bin/env python3
"""Inspect (default) or explicitly reclaim abandoned direct-R2 staging entries.

Run from the repository with PYTHONPATH=src, or the installed Katcha environment.
No scheduled timer is installed by this command.
"""

from __future__ import annotations

import argparse
import json

from katcha.ops.direct_staging_reconciliation import reconcile_direct_staging


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Conservative reconciliation for staging/direct/ only"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Delete eligible aged objects and abort incomplete uploads (default: read-only)",
    )
    parser.add_argument(
        "--min-age-hours", type=int, default=24,
        help="Minimum source age, never below 24h",
    )
    parser.add_argument(
        "--max-records", type=int, default=200,
        help="Maximum entries scanned per inventory, 1 through 2000",
    )
    args = parser.parse_args()
    summary = reconcile_direct_staging(
        apply=args.apply,
        min_age_hours=args.min_age_hours,
        max_records=args.max_records,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
