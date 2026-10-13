"""Offline search-evidence recovery commands; never dispatches provider work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from katcha.db import engine
from katcha.editorial.source_intelligence.postgres_jobs import (
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
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
