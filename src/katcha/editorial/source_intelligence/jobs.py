"""Durable, conservative dispatch ledger for source-search activities.

The ledger is an activity-local repository, not a replacement Clip Library or
rights authority. Its path must be on persistent storage. The caller must first
authorize the channel/project scope using the existing application authority.
All monetary amounts are integer microdollars. Reservations are never released
on uncertain outcomes, so crash recovery cannot accidentally repeat paid work.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from katcha.acquisition.adapters import DiscoveryAdapter, DiscoveryProviderError, get_adapter
from katcha.editorial.source_intelligence.candidates import (
    AssetCandidateV1,
    candidate_from_provider,
)
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchReceiptV1,
    SearchScopeV1,
    SourceSearchJobV1,
    bounded_cursor,
    canonical,
    digest,
)
from katcha.editorial.source_intelligence.planner import (
    SearchCapability,
    adapter_query,
    search_capability,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _units(value: Decimal) -> int:
    return int(value * 1_000_000)


class SearchJobLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS source_search_ledger_identity (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    ledger_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_search_candidates (
                    job_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (job_id, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS source_search_budgets (
                    scope_key TEXT NOT NULL, provider TEXT NOT NULL,
                    policy TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
                    results INTEGER NOT NULL DEFAULT 0, cost INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (scope_key, provider)
                );
                CREATE TABLE IF NOT EXISTS source_search_jobs (
                    job_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL,
                    intent TEXT NOT NULL, receipt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_search_receipt_events (
                    event_id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL,
                    job_id TEXT NOT NULL, receipt TEXT NOT NULL
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO source_search_ledger_identity VALUES (1, ?)",
                (str(uuid.uuid4()),),
            )

    @property
    def identity(self) -> str:
        """Durable volume identity; a missing/replaced ledger must fail closed."""
        with self._db() as db:
            row = db.execute(
                "SELECT ledger_id FROM source_search_ledger_identity WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("Search ledger identity is missing; restore its evidence volume")
            return str(row["ledger_id"])

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db

    def read(self, job_id: str, *, scope: SearchScopeV1) -> SearchReceiptV1 | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM source_search_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            if row["scope_key"] != scope.key:
                raise PermissionError("search receipt belongs to another scope")
            return SearchReceiptV1.model_validate_json(row["receipt"])

    def candidates(self, job_id: str, *, scope: SearchScopeV1) -> tuple[AssetCandidateV1, ...]:
        """Read only after application authorization of the requested scope."""
        self.read(job_id, scope=scope)
        with self._db() as db:
            rows = db.execute(
                "SELECT payload FROM source_search_candidates WHERE job_id=? AND scope_key=? "
                "ORDER BY candidate_id",
                (job_id, scope.key),
            ).fetchall()
            return tuple(AssetCandidateV1.model_validate_json(row["payload"]) for row in rows)

    def history(self, job_id: str, *, scope: SearchScopeV1) -> tuple[SearchReceiptV1, ...]:
        self.read(job_id, scope=scope)
        with self._db() as db:
            rows = db.execute(
                "SELECT receipt FROM source_search_receipt_events "
                "WHERE job_id=? AND scope_key=? ORDER BY event_id",
                (job_id, scope.key),
            ).fetchall()
            return tuple(SearchReceiptV1.model_validate_json(row["receipt"]) for row in rows)

    def _claim(
        self,
        job: SourceSearchJobV1,
        policy: ProviderPolicyV1,
        cap: SearchCapability,
        *,
        retry: bool,
    ) -> tuple[SearchReceiptV1, bool]:
        job.verify_identity()
        intent = canonical(job.model_dump(mode="json"))
        provider = job.provider_key + "@" + job.provider_version
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM source_search_jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            attempt = 1
            if previous:
                if previous["intent"] != intent:
                    raise ValueError("idempotency collision")
                receipt = SearchReceiptV1.model_validate_json(previous["receipt"])
                if not retry or receipt.status != "rate_limited":
                    return receipt, False
                if receipt.attempt >= job.max_attempts:
                    return receipt, False
                deadline = datetime.fromisoformat(receipt.finished_at or receipt.started_at)
                deadline += timedelta(seconds=receipt.retry_after_seconds or 0)
                if _now() < deadline:
                    return receipt, False
                attempt = receipt.attempt + 1

            error = None
            if job.policy_version != policy.version or job.max_attempts > policy.max_attempts:
                error = "policy_mismatch"
            elif (
                (cap.provider_key, cap.provider_version) != (job.provider_key, job.provider_version)
                or cap.query_style is None
                or job.max_results > cap.page_limit
            ):
                error = "provider_capability_mismatch"
            elif job.negative_terms and not cap.supports_negative_terms:
                error = "unsupported_negative_terms"
            elif cap.external and not policy.external_search_authorized:
                error = "external_search_not_authorized"
            elif cap.potentially_paid:
                # Existing web_scout enforces a global AI budget, but no proven
                # per-request ceiling. Planning is supported; dispatch is blocked.
                error = "paid_adapter_has_no_enforced_request_ceiling"
            elif job.max_cost != policy.call_cost_ceiling:
                error = "cost_reservation_mismatch"

            budget = db.execute(
                "SELECT * FROM source_search_budgets WHERE scope_key=? AND provider=?",
                (job.scope.key, provider),
            ).fetchone()
            frozen_policy = canonical(policy.model_dump(mode="json"))
            if budget and budget["policy"] != frozen_policy:
                error = "policy_changed_for_existing_budget"
            status: Any = "policy_blocked" if error else "running"
            if not error:
                calls = budget["calls"] if budget else 0
                results = budget["results"] if budget else 0
                cost = budget["cost"] if budget else 0
                if (
                    calls + 1 > policy.max_calls
                    or results + job.max_results > policy.max_results
                    or cost + _units(job.max_cost) > _units(policy.max_cost)
                ):
                    status, error = "quota_exceeded", "provider_budget_exhausted"
                else:
                    db.execute(
                        """
                        INSERT INTO source_search_budgets
                            (scope_key, provider, policy, calls, results, cost) VALUES (?,?,?,?,?,?)
                        ON CONFLICT(scope_key, provider) DO UPDATE SET
                            calls=excluded.calls, results=excluded.results, cost=excluded.cost
                    """,
                        (
                            job.scope.key,
                            provider,
                            frozen_policy,
                            calls + 1,
                            results + job.max_results,
                            cost + _units(job.max_cost),
                        ),
                    )
            receipt = SearchReceiptV1(
                receipt_id="ssr_" + digest([job.job_id, attempt]),
                job_id=job.job_id,
                scope=job.scope,
                status=status,
                attempt=attempt,
                started_at=_now().isoformat(),
                finished_at=_now().isoformat() if error else None,
                error_category=error,
                reserved_cost=job.max_cost if not error else Decimal("0"),
                next_action="Review provider policy or allocate a new authorized budget."
                if error
                else None,
            )
            db.execute(
                """
                INSERT INTO source_search_jobs (job_id, scope_key, intent, receipt) VALUES (?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET receipt=excluded.receipt
            """,
                (job.job_id, job.scope.key, intent, receipt.model_dump_json()),
            )
            db.execute(
                "INSERT INTO source_search_receipt_events "
                "(scope_key, job_id, receipt) VALUES (?,?,?)",
                (job.scope.key, job.job_id, receipt.model_dump_json()),
            )
            return receipt, not error

    def _finish(
        self,
        receipt: SearchReceiptV1,
        *,
        candidates: tuple[AssetCandidateV1, ...] = (),
        **changes: Any,
    ) -> SearchReceiptV1:
        result = SearchReceiptV1.model_validate(
            {
                **receipt.model_dump(),
                **changes,
                "finished_at": _now().isoformat(),
            }
        )
        with self._db() as db:
            # A second worker/reconciler cannot overwrite a newer terminal state.
            updated = db.execute(
                "UPDATE source_search_jobs SET receipt=? WHERE job_id=? AND receipt=?",
                (result.model_dump_json(), receipt.job_id, receipt.model_dump_json()),
            )
            if updated.rowcount != 1:
                raise RuntimeError("search receipt changed during dispatch")
            for candidate in candidates:
                db.execute(
                    "INSERT INTO source_search_candidates "
                    "(job_id, candidate_id, scope_key, payload) VALUES (?,?,?,?)",
                    (
                        result.job_id,
                        candidate.candidate_id,
                        result.scope.key,
                        candidate.model_dump_json(),
                    ),
                )
            db.execute(
                "INSERT INTO source_search_receipt_events "
                "(scope_key, job_id, receipt) VALUES (?,?,?)",
                (result.scope.key, result.job_id, result.model_dump_json()),
            )
        return result

    def mark_interrupted(self, job_id: str, *, scope: SearchScopeV1) -> SearchReceiptV1:
        """Recovery only after the caller confirms the activity worker has stopped."""
        receipt = self.read(job_id, scope=scope)
        if receipt is None:
            raise KeyError(job_id)
        if receipt.status != "running":
            return receipt
        return self._finish(
            receipt,
            status="unknown",
            error_category="worker_interrupted",
            next_action="Reconcile provider outcome; do not automatically retry.",
        )

    def dispatch(
        self,
        job: SourceSearchJobV1,
        *,
        scope: SearchScopeV1,
        policy: ProviderPolicyV1,
        adapter: DiscoveryAdapter | None = None,
        capability: SearchCapability | None = None,
        retry_rate_limit: bool = False,
    ) -> SearchReceiptV1:
        """Execute one page. Adapters own bounded transport timeouts.

        Injected adapters/capabilities are trusted composition-root configuration,
        never accepted from an API payload. No thread timeout is used: it could
        leave a billable request running while a replacement request is issued.
        """
        if job.scope != scope:
            raise PermissionError("job belongs to another channel/project/revision")
        try:
            resolved = adapter or get_adapter(job.provider_key, job.provider_version)
        except ValueError:
            cap = SearchCapability(job.provider_key, job.provider_version, None, 0)
            receipt, _ = self._claim(job, policy, cap, retry=False)
            return receipt
        if (resolved.key, resolved.version) != (job.provider_key, job.provider_version):
            raise ValueError("adapter does not match job provider")
        cap = capability or search_capability(resolved)
        receipt, execute = self._claim(job, policy, cap, retry=retry_rate_limit)
        if not execute:
            return receipt
        try:
            batch = resolved.discover(adapter_query(job, cap), dict(job.cursor))
            if len(batch.items) > job.max_results:
                raise ValueError("provider exceeded requested result bound")
            cursor = bounded_cursor(batch.next_cursor)
            if not batch.done and (not cursor or cursor == job.cursor):
                raise ValueError("provider did not advance its cursor")
            normalized: dict[str, AssetCandidateV1] = {}
            for item in batch.items:
                candidate = candidate_from_provider(item, job, receipt)
                normalized.setdefault(candidate.candidate_id, candidate)
            candidates = tuple(normalized.values())
            urls = tuple(dict.fromkeys(c.original_url for c in candidates))
            usage = batch.provider_usage
            if len(usage) > 30 or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0 or len(key) > 100
                for key, value in usage.items()
            ):
                raise ValueError("invalid provider usage receipt")
            return self._finish(
                receipt,
                status="succeeded",
                source_urls=urls,
                candidates=candidates,
                next_cursor=cursor,
                done=batch.done,
                provider_usage=usage,
            )
        except DiscoveryProviderError as exc:
            # Only a known 429 rejection is safe for an explicit bounded retry.
            rate_limited = exc.kind == "rate_limited" and exc.status_code == 429
            retry_delay = max(exc.retry_after_seconds or 0, 0)
            excessive_delay = rate_limited and retry_delay > 86400
            if excessive_delay:
                rate_limited = False
            known_failure = exc.status_code is not None and 400 <= exc.status_code < 500
            status = "rate_limited" if rate_limited else "failed" if known_failure else "unknown"
            return self._finish(
                receipt,
                status=status,
                error_category="retry_delay_exceeds_policy" if excessive_delay else exc.kind[:100],
                provider_usage={
                    key[:100]: min(max(value, 0), 2**63 - 1)
                    for key, value in list(exc.provider_usage.items())[:30]
                },
                retry_after_seconds=retry_delay if rate_limited else None,
                next_action="Retry after the provider delay within the existing budget."
                if rate_limited
                else "Review provider configuration or reconcile request outcome.",
            )
        except Exception:
            # Do not retain arbitrary exception strings (credentials/response bodies).
            # A malformed response may follow a charged request, so hold its reservation.
            return self._finish(
                receipt,
                status="unknown",
                error_category="unconfirmed_outcome",
                next_action="Reconcile provider outcome; do not automatically retry.",
            )
