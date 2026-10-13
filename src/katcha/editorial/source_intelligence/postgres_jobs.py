"""PostgreSQL storage for the existing conservative search receipt state machine.

Every read and mutation serializes on a single durable head. Provider I/O happens
only after the inherited claim transaction commits and outside database locks.
Runtime never creates schema or evidence. Recovery activation is an offline,
operator-reviewed statement of completeness, not an automatic retry mechanism.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from katcha.editorial.source_intelligence.candidates import AssetCandidateV1
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchReceiptV1,
    SourceSearchJobV1,
    digest,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.search_ledger_models import SOURCE_LEDGER_COLUMNS

HEAD = "source_search_ledger_head"
DEFAULT_MAX_EVIDENCE_ROWS = 1_000_000


class SearchLedgerRecoveryError(ValueError):
    """Evidence cannot be proven complete; external dispatch must stay disabled."""


def _postgres(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise ValueError("Authoritative search evidence requires PostgreSQL")


def _durable(connection: Connection) -> None:
    # A pooled session may have disabled synchronous commit. A claim must reach
    # durable local WAL before provider I/O; refuse servers without fsync.
    if connection.scalar(text("SHOW fsync")) != "on":
        raise SearchLedgerRecoveryError(
            "Search evidence requires PostgreSQL fsync; keep dispatch disabled"
        )
    connection.execute(text("SET LOCAL synchronous_commit = 'on'"))


def _seal(
    connection: Connection,
    head: Mapping[str, Any],
    *,
    max_evidence_rows: int = DEFAULT_MAX_EVIDENCE_ROWS,
) -> str:
    # recovery_required is deliberately excluded: restore can inhibit dispatch
    # monotonically without possessing/recalculating an evidence integrity seal.
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            [1, head["ledger_id"], head["recovery_generation"], head["activation_evidence"]],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    )
    total = 0
    for table, columns in SOURCE_LEDGER_COLUMNS.items():
        count = connection.scalar(text(f"SELECT COUNT(*) FROM {table}"))
        total += count
        if total > max_evidence_rows:
            raise SearchLedgerRecoveryError(
                "Search evidence exceeds the configured integrity scan bound; "
                "review capacity before dispatch"
            )
        # C collation makes ordering independent of restore-cluster locale. Cast
        # all values to text for a stable cross-version ordering including IDs.
        order = ",".join(f'CAST({column} AS TEXT) COLLATE "C"' for column in columns)
        rows = connection.execute(
            text(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}"),
            execution_options={"stream_results": True},
        )
        digest.update(json.dumps([table, columns], separators=(",", ":")).encode())
        for row in rows:
            payload = json.dumps(list(row), ensure_ascii=True, separators=(",", ":")).encode()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


def _lock_and_verify(
    connection: Connection, *, max_evidence_rows: int = DEFAULT_MAX_EVIDENCE_ROWS
) -> dict[str, Any]:
    rows = connection.execute(text(f"SELECT * FROM {HEAD} FOR UPDATE")).mappings().all()
    if len(rows) != 1 or rows[0]["singleton"] != 1:
        raise SearchLedgerRecoveryError(
            "Search ledger is uninitialized; restore or import saved evidence"
        )
    head = dict(rows[0])
    identity = (
        connection.execute(text("SELECT singleton, ledger_id FROM source_search_ledger_identity"))
        .mappings()
        .all()
    )
    if (
        len(identity) != 1
        or identity[0]["singleton"] != 1
        or identity[0]["ledger_id"] != head["ledger_id"]
    ):
        raise SearchLedgerRecoveryError(
            "Search ledger identity is incomplete; restore saved evidence"
        )
    if _seal(connection, head, max_evidence_rows=max_evidence_rows) != head["integrity_sha256"]:
        raise SearchLedgerRecoveryError(
            "Search evidence integrity mismatch; "
            "keep dispatch disabled and restore complete evidence"
        )
    return head


def _report(connection: Connection, head: Mapping[str, Any]) -> dict[str, Any]:
    legacy = {}
    for line in head["activation_evidence"].splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") == "legacy_import":
            legacy = event
    return {
        "metadata_gaps": [
            {"job_id": job_id, "category": "legacy_candidate_metadata_unavailable"}
            for job_id in legacy.get("metadata_gap_job_ids", [])
        ],
        "identity_assigned_for_pre_identity": legacy.get("identity_assigned", False),
        **dict(head),
        "counts": {
            table: connection.scalar(text(f"SELECT COUNT(*) FROM {table}"))
            for table in SOURCE_LEDGER_COLUMNS
        },
    }


def _save_seal(
    connection: Connection,
    head: Mapping[str, Any],
    *,
    max_evidence_rows: int = DEFAULT_MAX_EVIDENCE_ROWS,
) -> str:
    seal = _seal(connection, head, max_evidence_rows=max_evidence_rows)
    connection.execute(
        text(f"UPDATE {HEAD} SET integrity_sha256=:seal WHERE singleton=1"), {"seal": seal}
    )
    return seal


class _Result:
    def __init__(self, result: Any) -> None:
        self.rowcount = result.rowcount
        self._result = result

    def fetchone(self) -> Mapping[str, Any] | None:
        return self._result.mappings().fetchone()

    def fetchall(self) -> list[Mapping[str, Any]]:
        return list(self._result.mappings().fetchall())


class _ConnectionAdapter:
    """Small adapter for the ledger's fixed SQL statements, not general SQL."""

    def __init__(
        self, connection: Connection, head: Mapping[str, Any], ledger: PostgresSearchJobLedger
    ) -> None:
        self.connection = connection
        self.head = head
        self.ledger = ledger
        self.mutated = False

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> _Result:
        normalized = " ".join(sql.split())
        if normalized == "BEGIN IMMEDIATE":
            # _db already acquired the head lock and verified evidence. Check the
            # gate in this same transaction, just before inherited _claim logic.
            if not self.ledger.dispatch_enabled:
                raise SearchLedgerRecoveryError(
                    "Search dispatch is disabled by server configuration"
                )
            if self.head["recovery_required"]:
                raise SearchLedgerRecoveryError(
                    "Search evidence requires offline recovery review before dispatch"
                )
            if self.head["recovery_generation"] != self.ledger.recovery_generation:
                raise SearchLedgerRecoveryError(
                    "Search recovery generation changed; "
                    "reconcile the backup-to-loss interval before dispatch"
                )
            return _Result(self.connection.execute(text("SELECT 1 WHERE FALSE")))
        if normalized.startswith("INSERT INTO source_search_receipt_events"):
            # SQLite allocated INTEGER PRIMARY KEY implicitly. Allocate under the
            # common lock; imported high historical IDs remain authoritative.
            event_id = self.connection.scalar(
                text("SELECT COALESCE(MAX(event_id),0)+1 FROM source_search_receipt_events")
            )
            sql = sql.replace(
                "(scope_key, job_id, receipt)", "(event_id, scope_key, job_id, receipt)"
            )
            sql = sql.replace("VALUES (?,?,?)", "VALUES (?,?,?,?)")
            parameters = (event_id, *parameters)
        parts = sql.split("?")
        if len(parts) != len(parameters) + 1:
            raise ValueError("Unsupported search ledger SQL bindings")
        translated = parts[0]
        bindings = {}
        for index, value in enumerate(parameters):
            key = f"p{index}"
            translated += f":{key}" + parts[index + 1]
            bindings[key] = value
        self.mutated |= normalized.split(" ", 1)[0] in {"INSERT", "UPDATE", "DELETE"}
        return _Result(self.connection.execute(text(translated), bindings))


class PostgresSearchJobLedger(SearchJobLedger):
    def __init__(
        self,
        engine: Engine,
        *,
        recovery_generation: int,
        dispatch_enabled: bool = False,
        max_evidence_rows: int = DEFAULT_MAX_EVIDENCE_ROWS,
    ) -> None:
        _postgres(engine)
        if isinstance(recovery_generation, bool) or recovery_generation < 1:
            raise ValueError("A positive independently configured recovery generation is required")
        if isinstance(max_evidence_rows, bool) or max_evidence_rows < 1:
            raise ValueError("Integrity scan bound must be positive")
        self.max_evidence_rows = max_evidence_rows
        self.engine = engine
        self.recovery_generation = recovery_generation
        self.dispatch_enabled = dispatch_enabled
        # No runtime schema creation, metadata initialization or fallback.

    @contextmanager
    def _db(self) -> Iterator[_ConnectionAdapter]:
        try:
            with self.engine.begin() as connection:
                _durable(connection)
                head = _lock_and_verify(connection, max_evidence_rows=self.max_evidence_rows)
                adapter = _ConnectionAdapter(connection, head, self)
                yield adapter
                if adapter.mutated:
                    _save_seal(connection, head, max_evidence_rows=self.max_evidence_rows)
        except ValidationError as exc:
            raise SearchLedgerRecoveryError(
                "Saved search evidence could not be validated; keep dispatch disabled"
            ) from exc
        except SQLAlchemyError as exc:
            # Omit server/connection strings, SQL and parameters from user-facing
            # errors; availability errors never trigger alternate evidence stores.
            raise SearchLedgerRecoveryError(
                "Search evidence database unavailable or incomplete; dispatch is blocked"
            ) from exc


def inspect_postgres_ledger(engine: Engine) -> dict[str, Any]:
    _postgres(engine)
    try:
        with engine.begin() as connection:
            _durable(connection)
            return _report(connection, _lock_and_verify(connection))
    except SQLAlchemyError as exc:
        raise SearchLedgerRecoveryError(
            "Search evidence database unavailable or incomplete"
        ) from exc


def quarantine_postgres_ledger(engine: Engine) -> dict[str, Any]:
    """Offline restore inhibition. Does not repair, initialize or authorize work."""
    _postgres(engine)
    with engine.begin() as connection:
        _durable(connection)
        # Inhibit before verification; missing/inconsistent head still cannot run.
        connection.execute(text(f"UPDATE {HEAD} SET recovery_required=TRUE"))
    return inspect_postgres_ledger(engine)


def activate_postgres_ledger(
    engine: Engine,
    *,
    expected_identity: str,
    expected_seal: str,
    expected_generation: int,
    recovery_generation: int,
    completeness_evidence: str,
    confirmed_quiesced: bool = False,
) -> dict[str, Any]:
    """Explicit offline attestation after complete import/recovery, never automatic.

    A restored backup alone is insufficient evidence: post-backup provider calls
    must be independently reconstructed/reconciled before this can be invoked.
    This API records an operator assertion, it cannot establish external facts.
    """
    _postgres(engine)
    if not confirmed_quiesced or not completeness_evidence.strip():
        raise SearchLedgerRecoveryError(
            "Quiesced workers and recorded completeness evidence are required"
        )
    if isinstance(recovery_generation, bool) or recovery_generation < 1:
        raise ValueError("Recovery generation must be positive")
    with engine.begin() as connection:
        _durable(connection)
        head = _lock_and_verify(connection)
        if (head["ledger_id"], head["integrity_sha256"], head["recovery_generation"]) != (
            expected_identity,
            expected_seal,
            expected_generation,
        ):
            raise SearchLedgerRecoveryError("Reviewed identity/seal changed; activation refused")
        if recovery_generation < head["recovery_generation"]:
            raise SearchLedgerRecoveryError("Recovery generation cannot move backwards")
        activation = json.dumps(
            {
                "activated_at": datetime.now(UTC).isoformat(),
                "generation": recovery_generation,
                "previous_seal": expected_seal,
                "completeness_evidence": completeness_evidence.strip(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        evidence = head["activation_evidence"]
        head.update(
            recovery_generation=recovery_generation,
            activation_evidence=(evidence + "\n" if evidence else "") + activation,
            recovery_required=False,
        )
        connection.execute(
            text(
                f"UPDATE {HEAD} SET recovery_generation=:generation, "
                "activation_evidence=:evidence, recovery_required=FALSE WHERE singleton=1"
            ),
            {
                "generation": recovery_generation,
                "evidence": head["activation_evidence"],
            },
        )
        head["integrity_sha256"] = _save_seal(connection, head)
        return _report(connection, head)


def _validate_snapshot(
    snapshot: dict[str, list[dict[str, Any]]],
    *,
    legacy_metadata_gap_job_ids: frozenset[str] = frozenset(),
) -> str:
    identity = snapshot["source_search_ledger_identity"]
    if len(identity) != 1 or identity[0]["singleton"] != 1 or not identity[0]["ledger_id"]:
        raise SearchLedgerRecoveryError(
            "SQLite evidence identity missing; do not create a replacement"
        )
    jobs = {}
    receipts = {}
    for row in snapshot["source_search_jobs"]:
        job = SourceSearchJobV1.model_validate_json(row["intent"])
        job.verify_identity()
        receipt = SearchReceiptV1.model_validate_json(row["receipt"])
        if (
            row["job_id"] != job.job_id
            or row["scope_key"] != job.scope.key
            or receipt.job_id != job.job_id
            or receipt.scope != job.scope
        ):
            raise SearchLedgerRecoveryError("Historical job/receipt scope mismatch")
        jobs[job.job_id] = job
        receipts[job.job_id] = receipt
    candidate_urls: dict[str, set[str]] = {}
    for row in snapshot["source_search_candidates"]:
        candidate = AssetCandidateV1.model_validate_json(row["payload"])
        job = jobs.get(row["job_id"])
        receipt = receipts.get(row["job_id"])
        if (
            job is None
            or row["scope_key"] != job.scope.key
            or candidate.candidate_id != row["candidate_id"]
            or candidate.scope != job.scope
        ):
            raise SearchLedgerRecoveryError("Historical candidate evidence mismatch")
        if (
            receipt.status != "succeeded"
            or candidate.scout_receipts != (receipt.receipt_id,)
            or (candidate.provider, candidate.provider_version)
            != (job.provider_key, job.provider_version)
        ):
            raise SearchLedgerRecoveryError("Historical candidate receipt binding mismatch")
        candidate_urls.setdefault(job.job_id, set()).add(candidate.original_url)
    for job_id in legacy_metadata_gap_job_ids:
        receipt = receipts.get(job_id)
        if (
            receipt is None
            or receipt.status != "succeeded"
            or not receipt.source_urls
            or candidate_urls.get(job_id)
        ):
            raise SearchLedgerRecoveryError(
                "Reviewed legacy metadata gap does not match retained evidence"
            )
    for job_id, receipt in receipts.items():
        if job_id in legacy_metadata_gap_job_ids:
            continue
        if candidate_urls.get(job_id, set()) != set(receipt.source_urls):
            raise SearchLedgerRecoveryError("Historical candidate coverage is incomplete")
    latest: dict[str, str] = {}
    previous: dict[str, SearchReceiptV1] = {}
    reservations: dict[tuple[str, str], list[int]] = {}
    claimed_jobs: set[str] = set()
    for row in sorted(
        snapshot["source_search_receipt_events"], key=lambda value: value["event_id"]
    ):
        receipt = SearchReceiptV1.model_validate_json(row["receipt"])
        job = jobs.get(row["job_id"])
        if (
            job is None
            or row["scope_key"] != job.scope.key
            or receipt.job_id != job.job_id
            or receipt.scope != job.scope
        ):
            raise SearchLedgerRecoveryError("Historical receipt event mismatch")
        if (
            receipt.receipt_id != "ssr_" + digest([job.job_id, receipt.attempt])
            or not 1 <= receipt.attempt <= job.max_attempts
        ):
            raise SearchLedgerRecoveryError("Historical receipt identity mismatch")
        prior = previous.get(job.job_id)
        if prior is None:
            if receipt.attempt != 1 or receipt.status not in {
                "running",
                "policy_blocked",
                "quota_exceeded",
            }:
                raise SearchLedgerRecoveryError("Historical claim event is missing")
        elif prior.status == "running":
            if (
                receipt.status == "running"
                or receipt.attempt != prior.attempt
                or receipt.started_at != prior.started_at
                or receipt.reserved_cost != prior.reserved_cost
            ):
                raise SearchLedgerRecoveryError(
                    "Historical terminal event does not match its claim"
                )
        elif prior.status == "rate_limited":
            if receipt.attempt != prior.attempt + 1 or receipt.status not in {
                "running",
                "policy_blocked",
                "quota_exceeded",
            }:
                raise SearchLedgerRecoveryError("Historical retry event is inconsistent")
        else:
            raise SearchLedgerRecoveryError("Historical terminal receipt was overwritten")
        if receipt.status == "running":
            claimed_jobs.add(job.job_id)
            if receipt.reserved_cost != job.max_cost:
                raise SearchLedgerRecoveryError("Historical cost reservation mismatch")
            key = (job.scope.key, job.provider_key + "@" + job.provider_version)
            totals = reservations.setdefault(key, [0, 0, 0])
            totals[0] += 1
            totals[1] += job.max_results
            totals[2] += int(receipt.reserved_cost * 1_000_000)
        latest[row["job_id"]] = row["receipt"]
        previous[row["job_id"]] = receipt
    for row in snapshot["source_search_jobs"]:
        if latest.get(row["job_id"]) != row["receipt"]:
            raise SearchLedgerRecoveryError("Historical receipt event history is incomplete")
    budgets = {}
    for row in snapshot["source_search_budgets"]:
        policy = ProviderPolicyV1.model_validate_json(row["policy"])
        if any(
            not isinstance(row[key], int) or not 0 <= row[key] <= 2**63 - 1
            for key in ("calls", "results", "cost")
        ):
            raise SearchLedgerRecoveryError("Historical budget is invalid")
        key = (row["scope_key"], row["provider"])
        if [row[key] for key in ("calls", "results", "cost")] != reservations.get(key):
            raise SearchLedgerRecoveryError("Historical budget does not match retained claims")
        budgets[key] = policy
    if set(budgets) != set(reservations):
        raise SearchLedgerRecoveryError("Historical claim budget is missing")
    for job_id in previous:
        job = jobs[job_id]
        key = (job.scope.key, job.provider_key + "@" + job.provider_version)
        if job_id in claimed_jobs:
            policy = budgets[key]
            if (
                job.policy_version != policy.version
                or job.max_cost != policy.call_cost_ceiling
                or job.max_attempts > policy.max_attempts
            ):
                raise SearchLedgerRecoveryError("Historical claim policy mismatch")
    return str(identity[0]["ledger_id"])


def import_sqlite_ledger(
    engine: Engine,
    path: Path,
    *,
    recovery_generation: int,
    confirmed_quiesced: bool = False,
    legacy_evidence_ref: str | None = None,
    legacy_metadata_gap_job_ids: tuple[str, ...] = (),
    allow_pre_identity: bool = False,
) -> dict[str, Any]:
    """WAL-consistent historical import into an empty, migrated PostgreSQL target.

    Operators must stop all legacy writers for the entire snapshot/cutover. SQLite
    backup protects WAL consistency but cannot itself prove that workers stopped.
    The source is read-only and retained; any import failure rolls back all rows.
    """
    _postgres(engine)
    if not confirmed_quiesced:
        raise SearchLedgerRecoveryError(
            "Stop all search workers and confirm quiesced cutover before import"
        )
    if isinstance(recovery_generation, bool) or recovery_generation < 1:
        raise ValueError("Recovery generation must be positive")
    if not path.is_file():
        raise SearchLedgerRecoveryError(
            "Historical SQLite ledger missing; restore it, never replace it"
        )
    if (allow_pre_identity or legacy_metadata_gap_job_ids) and not legacy_evidence_ref:
        raise SearchLedgerRecoveryError("Explicit reviewed legacy provenance is required")
    if legacy_evidence_ref is not None and (
        not isinstance(legacy_evidence_ref, str)
        or not legacy_evidence_ref.strip()
        or len(legacy_evidence_ref) > 1024
    ):
        raise SearchLedgerRecoveryError("Legacy provenance must be a bounded evidence reference")
    if len(set(legacy_metadata_gap_job_ids)) != len(legacy_metadata_gap_job_ids):
        raise SearchLedgerRecoveryError("Duplicate legacy metadata gap identities")
    snapshot = {}
    missing_tables = []
    # URI mode=ro refuses accidental database creation; backup includes committed WAL.
    with (
        closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(":memory:")) as copy,
    ):
        source.backup(copy)
        copy.row_factory = sqlite3.Row
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SearchLedgerRecoveryError("SQLite evidence integrity check failed")
        try:
            present = {
                row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing_tables = sorted(set(SOURCE_LEDGER_COLUMNS) - present)
            permitted = {"source_search_candidates"} if legacy_evidence_ref else set()
            if allow_pre_identity:
                permitted.add("source_search_ledger_identity")
            if set(missing_tables) - permitted:
                raise SearchLedgerRecoveryError("Historical SQLite evidence schema is incomplete")
            total = sum(
                copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in SOURCE_LEDGER_COLUMNS
                if table in present
            )
            if total > DEFAULT_MAX_EVIDENCE_ROWS:
                raise SearchLedgerRecoveryError("Historical evidence exceeds the import scan bound")
            for table, columns in SOURCE_LEDGER_COLUMNS.items():
                snapshot[table] = (
                    [dict(row) for row in copy.execute(f"SELECT {','.join(columns)} FROM {table}")]
                    if table in present
                    else []
                )
        except sqlite3.DatabaseError as exc:
            raise SearchLedgerRecoveryError(
                "Historical SQLite evidence schema is incomplete"
            ) from exc
    source_sha256 = hashlib.sha256(
        json.dumps(
            {
                "missing_tables": missing_tables,
                "rows": {
                    table: sorted(json.dumps(row, sort_keys=True) for row in rows)
                    for table, rows in snapshot.items()
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    identity_assigned = "source_search_ledger_identity" in missing_tables
    if identity_assigned:
        snapshot["source_search_ledger_identity"] = [
            {
                "singleton": 1,
                "ledger_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, "katcha-legacy-search-ledger:" + source_sha256)
                ),
            }
        ]
    try:
        ledger_id = _validate_snapshot(
            snapshot, legacy_metadata_gap_job_ids=frozenset(legacy_metadata_gap_job_ids)
        )
    except ValidationError as exc:
        raise SearchLedgerRecoveryError(
            "Historical search evidence could not be validated"
        ) from exc
    legacy_attestation = ""
    if legacy_evidence_ref:
        legacy_attestation = (
            json.dumps(
                {
                    "kind": "legacy_import",
                    "source_sha256": source_sha256,
                    "missing_tables": missing_tables,
                    "identity_assigned": identity_assigned,
                    "metadata_gap_job_ids": sorted(legacy_metadata_gap_job_ids),
                    "asserted_provenance_ref": legacy_evidence_ref.strip(),
                },
                sort_keys=True,
            )
            + "\n"
        )
    with engine.begin() as connection:
        _durable(connection)
        # The target has no head yet; table locks serialize concurrent importers.
        names = [HEAD, *SOURCE_LEDGER_COLUMNS]
        connection.execute(text("LOCK TABLE " + ",".join(names) + " IN ACCESS EXCLUSIVE MODE"))
        if any(connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) for table in names):
            raise SearchLedgerRecoveryError(
                "Target search evidence is populated; import refused without overwrite"
            )
        for table, columns in SOURCE_LEDGER_COLUMNS.items():
            for row in snapshot[table]:
                connection.execute(
                    text(
                        f"INSERT INTO {table} ({','.join(columns)}) "
                        f"VALUES ({','.join(':' + column for column in columns)})"
                    ),
                    row,
                )
        head = {
            "singleton": 1,
            "ledger_id": ledger_id,
            "recovery_generation": recovery_generation,
            "recovery_required": True,
            "activation_evidence": legacy_attestation,
            "integrity_sha256": "",
        }
        head["integrity_sha256"] = _seal(connection, head)
        connection.execute(
            text(
                f"INSERT INTO {HEAD} ({','.join(head)}) "
                f"VALUES ({','.join(':' + key for key in head)})"
            ),
            head,
        )
        return _report(connection, head)
