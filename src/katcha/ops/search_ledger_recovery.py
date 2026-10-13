"""Offline search-evidence recovery commands; never dispatches provider work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from katcha.db import engine
from katcha.editorial.source_intelligence.postgres_jobs import (
    SearchLedgerRecoveryError,
    activate_postgres_ledger,
    import_sqlite_ledger,
    inspect_postgres_ledger,
    quarantine_postgres_ledger,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="Verify and report saved evidence; no activation")
    commands.add_parser("quarantine", help="Inhibit search dispatch after any database restore")
    migrate = commands.add_parser(
        "import-sqlite", help="Import retained WAL-consistent evidence atomically"
    )
    migrate.add_argument("--source", type=Path, required=True)
    migrate.add_argument("--generation", type=int, required=True)
    migrate.add_argument("--confirm-workers-stopped", action="store_true")
    migrate.add_argument(
        "--legacy-evidence-ref",
        help="Durable reviewed evidence record for a historical-format exception",
    )
    migrate.add_argument(
        "--legacy-metadata-gap-job-id",
        action="append",
        default=[],
        help="Exact succeeded job lacking historical candidate metadata; repeat for each gap",
    )
    migrate.add_argument(
        "--allow-pre-identity",
        action="store_true",
        help="Explicitly permit an attested pre-identity historical snapshot",
    )
    activate = commands.add_parser(
        "activate", help="Record separately reviewed completeness attestation"
    )
    activate.add_argument("--expected-identity", required=True)
    activate.add_argument("--expected-seal", required=True)
    activate.add_argument("--generation", type=int, required=True)
    activate.add_argument("--expected-generation", type=int, required=True)
    activate.add_argument("--completeness-record", required=True)
    activate.add_argument("--confirm-workers-stopped", action="store_true")
    args = parser.parse_args()
    legacy_options = {}
    if args.command == "import-sqlite":
        evidence = args.legacy_evidence_ref
        if evidence is not None and not evidence.strip():
            parser.error("--legacy-evidence-ref requires a nonempty durable evidence record")
        if (args.allow_pre_identity or args.legacy_metadata_gap_job_id) and not evidence:
            parser.error("historical exceptions require --legacy-evidence-ref")
        if len(set(args.legacy_metadata_gap_job_id)) != len(args.legacy_metadata_gap_job_id):
            parser.error("historical metadata gaps must list each exact job ID once")
        if evidence is not None:
            legacy_options["legacy_evidence_ref"] = evidence.strip()
        if args.legacy_metadata_gap_job_id:
            legacy_options["legacy_metadata_gap_job_ids"] = tuple(args.legacy_metadata_gap_job_id)
        if args.allow_pre_identity:
            legacy_options["allow_pre_identity"] = True
    try:
        if args.command == "inspect":
            report = inspect_postgres_ledger(engine)
        elif args.command == "quarantine":
            report = quarantine_postgres_ledger(engine)
        elif args.command == "import-sqlite":
            report = import_sqlite_ledger(
                engine,
                args.source,
                recovery_generation=args.generation,
                confirmed_quiesced=args.confirm_workers_stopped,
                **legacy_options,
            )
        else:
            report = activate_postgres_ledger(
                engine,
                expected_identity=args.expected_identity,
                expected_seal=args.expected_seal,
                recovery_generation=args.generation,
                expected_generation=args.expected_generation,
                completeness_evidence=args.completeness_record,
                confirmed_quiesced=args.confirm_workers_stopped,
            )
    except SearchLedgerRecoveryError as exc:
        parser.exit(2, f"Search evidence recovery refused: {exc}\n")
    except Exception:
        # Never expose arbitrary connection strings, SQL or row parameters in a
        # CLI traceback. A failed operation cannot authorize recovery or retries.
        parser.exit(
            2,
            "Search evidence recovery failed; keep dispatch disabled "
            "and inspect retained evidence.\n",
        )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
