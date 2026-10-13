"""Offline pre-seal import validation detects reconstructable historical gaps."""

import sqlite3
from copy import deepcopy
from uuid import UUID

import pytest

from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchIntentV1,
    SearchScopeV1,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability, plan_job
from katcha.editorial.source_intelligence.postgres_jobs import (
    SearchLedgerRecoveryError,
    _validate_snapshot,
)
from katcha.search_ledger_models import SOURCE_LEDGER_COLUMNS


@pytest.fixture
def historical(tmp_path):
    path = tmp_path / "historical.sqlite"
    ledger = SearchJobLedger(path)
    scope = SearchScopeV1(channel_id=UUID(int=1), project_id=UUID(int=2), revision_digest="a" * 64)
    cap = SearchCapability("fixture", "v1", "fixture", 10, external=False)
    policy = ProviderPolicyV1(version="fixture", max_calls=1)
    for requirement in ("one", "two"):
        job = plan_job(
            SearchIntentV1(
                scope=scope,
                requirement_id=requirement,
                query_terms=("fixture",),
                semantic_brief="Offline import fixture",
                source_purpose="official_reference",
                requested_by="fixture",
            ),
            provider_key="fixture",
            provider_version="v1",
            policy=policy,
            max_results=2,
            capability=cap,
        )
        receipt, claimed = ledger._claim(job, policy, cap, retry=False)
        if claimed:
            ledger._finish(receipt, status="unknown", error_category="fixture_interruption")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        snapshot = {
            table: [dict(row) for row in db.execute(f"SELECT {','.join(columns)} FROM {table}")]
            for table, columns in SOURCE_LEDGER_COLUMNS.items()
        }
    return snapshot, ledger.identity


def test_valid_historical_claim_terminal_and_quota_preserve_identity(historical):
    snapshot, identity = historical
    assert _validate_snapshot(snapshot) == identity
    assert [row["calls"] for row in snapshot["source_search_budgets"]] == [1]


@pytest.mark.parametrize("gap", ["budget", "old_claim", "terminal", "identity", "counter"])
def test_reconstructable_preseal_gaps_are_refused(historical, gap):
    snapshot, _ = historical
    snapshot = deepcopy(snapshot)
    if gap == "budget":
        snapshot["source_search_budgets"].clear()
    elif gap == "old_claim":
        snapshot["source_search_receipt_events"].pop(0)
    elif gap == "terminal":
        snapshot["source_search_receipt_events"].pop(1)
    elif gap == "identity":
        snapshot["source_search_ledger_identity"].clear()
    elif gap == "counter":
        snapshot["source_search_budgets"][0]["calls"] += 1
    with pytest.raises(SearchLedgerRecoveryError):
        _validate_snapshot(snapshot)
