"""Offline evidence for KSI-102: no network or credentials are used."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from katcha.acquisition.adapters import (
    DiscoveredCandidate,
    DiscoveryBatch,
    DiscoveryProviderError,
    get_adapter,
)
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchIntentV1,
    SearchScopeV1,
    SourceSearchJobV1,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import (
    SearchCapability,
    adapter_query,
    next_page,
    plan_job,
    search_capability,
)

CAP = SearchCapability(
    "fixture_search", "v1", "fixture", 100, external=False, supports_negative_terms=True
)
SCOPE = SearchScopeV1(channel_id=UUID(int=1), project_id=UUID(int=2), revision_digest="a" * 64)


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("KSI-102 tests must not contact real providers")

    monkeypatch.setattr(httpx.Client, "request", forbidden)


def intent(requirement="req-1", **changes):
    return SearchIntentV1(
        scope=SCOPE,
        requirement_id=requirement,
        query_terms=("VisionQuest", "official trailer"),
        negative_terms=("fan edit",),
        semantic_brief="Official source reference",
        source_purpose="official_reference",
        requested_by="fixture",
        **changes,
    )


def policy(**changes):
    return ProviderPolicyV1(version="fixture-policy-v1", **changes)


def job(requirement="req-1", *, p=None, max_results=2):
    return plan_job(
        intent(requirement),
        provider_key=CAP.provider_key,
        provider_version=CAP.provider_version,
        policy=p or policy(),
        max_results=max_results,
        capability=CAP,
    )


class FakeAdapter:
    key = CAP.provider_key
    version = CAP.provider_version

    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    "https://example.org/official/fixture", external_id="fixture-1"
                ),
            ),
            provider_usage={"fixture.search": 1},
        )

    def discover(self, query, cursor):
        self.calls.append((query, cursor))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def dispatch(ledger, j, adapter, p=None, **kwargs):
    return ledger.dispatch(
        j, scope=SCOPE, policy=p or policy(), adapter=adapter, capability=CAP, **kwargs
    )


def test_120_requirements_have_stable_serializable_jobs_and_no_parser_cap():
    jobs = [job(f"req-{i}") for i in range(120)]
    assert len({j.job_id for j in jobs}) == 120
    for i, planned in enumerate(jobs):
        assert planned == job(f"req-{i}")
        assert SourceSearchJobV1.model_validate_json(planned.model_dump_json()) == planned
        planned.verify_identity()
    other_scope = SCOPE.model_copy(update={"project_id": UUID(int=3)})
    other = plan_job(
        intent().model_copy(update={"scope": other_scope}),
        provider_key=CAP.provider_key,
        provider_version="v1",
        policy=policy(),
        max_results=2,
        capability=CAP,
    )
    assert other.job_id != jobs[1].job_id


@pytest.mark.parametrize(
    "change",
    [
        {"max_results": 0},
        {"max_results": True},
        {"max_results": 101},
        {"max_cost": "NaN"},
        {"max_cost": "-1"},
        {"max_attempts": 4},
        {"cursor": {"page": "x" * 4097}},
        {"cursor": {"api_key": "secret"}},
        {"cursor": {"a": {"b": {"c": {"d": {"e": "x"}}}}}},
        {"unknown_field": "ignored?"},
    ],
)
def test_payload_bounds_fail_closed(change):
    with pytest.raises((ValueError, ValidationError)):
        SourceSearchJobV1.model_validate({**job().model_dump(), **change})


def test_installed_capabilities_do_not_infer_download_rights():
    youtube = search_capability(get_adapter("youtube", "v1"))
    assert youtube.page_limit == 50 and youtube.availability == "reference_only"
    planned = plan_job(intent(), provider_key="youtube", provider_version="v1", policy=policy())
    query = adapter_query(planned, youtube)
    assert query["freshness_horizon_hours"] == 0
    assert query["order"] == "relevance"
    assert '-"fan edit"' in query["q"]
    assert search_capability(get_adapter("web_scout", "v1")).potentially_paid
    for provider in ("manifest", "rss_atom", "operator_feed"):
        with pytest.raises(ValueError, match="not semantic search"):
            plan_job(intent(), provider_key=provider, provider_version="v1", policy=policy())
    with pytest.raises(ValueError, match="negative"):
        plan_job(intent(), provider_key="reddit", provider_version="v1", policy=policy())


def test_success_and_replay_survive_reopening_ledger(tmp_path):
    path = tmp_path / "search.sqlite"
    fake = FakeAdapter()
    result = dispatch(SearchJobLedger(path), job(), fake)
    assert result.status == "succeeded" and result.provider_usage == {"fixture.search": 1}
    assert result.source_urls == ("https://example.org/official/fixture",)
    reopened = SearchJobLedger(path)
    assert dispatch(reopened, job(), fake) == result
    assert len(fake.calls) == 1
    assert [r.status for r in reopened.history(job().job_id, scope=SCOPE)] == [
        "running",
        "succeeded",
    ]


@pytest.mark.parametrize(
    "caps",
    [
        {"max_calls": 1},
        {"max_results": 2},
        {"max_cost": Decimal("0.1"), "call_cost_ceiling": Decimal("0.1")},
    ],
)
def test_budget_is_cumulative_across_jobs_and_restarts(tmp_path, caps):
    p = policy(**caps)
    fake = FakeAdapter()
    path = tmp_path / "search.sqlite"
    assert dispatch(SearchJobLedger(path), job(p=p), fake, p).status == "succeeded"
    blocked = dispatch(SearchJobLedger(path), job("req-2", p=p), fake, p)
    assert blocked.status == "quota_exceeded"
    assert dispatch(SearchJobLedger(path), job("req-2", p=p), fake, p) == blocked
    assert len(fake.calls) == 1


def test_concurrent_replay_executes_only_once(tmp_path):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    fake = FakeAdapter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: dispatch(ledger, job(), fake), range(8)))
    assert len(fake.calls) == 1
    assert {r.status for r in results} <= {"running", "succeeded"}
    assert ledger.read(job().job_id, scope=SCOPE).status == "succeeded"


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (TimeoutError("SECRET must not be logged"), "unknown"),
        (DiscoveryProviderError("secret", kind="transport_error"), "unknown"),
        (DiscoveryProviderError("secret", kind="provider_http_error", status_code=503), "unknown"),
        (
            DiscoveryProviderError("secret", kind="forbidden", status_code=403, transient=False),
            "failed",
        ),
    ],
)
def test_failures_are_durable_and_do_not_blindly_retry(tmp_path, outcome, expected):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    fake = FakeAdapter(outcome)
    result = dispatch(ledger, job(), fake)
    assert result.status == expected
    assert "SECRET" not in result.model_dump_json() and "secret" not in result.model_dump_json()
    assert dispatch(ledger, job(), fake, retry_rate_limit=True) == result
    assert len(fake.calls) == 1


def test_rate_limit_wait_and_attempt_ceiling_are_durable(tmp_path, monkeypatch):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    p = policy(max_attempts=2)
    j = job(p=p)
    fake = FakeAdapter(
        DiscoveryProviderError(
            "slow down",
            kind="rate_limited",
            status_code=429,
            retry_after_seconds=30,
            provider_usage={"search": 1},
        )
    )
    first = dispatch(ledger, j, fake, p)
    assert first.status == "rate_limited" and first.provider_usage == {"search": 1}
    assert dispatch(ledger, j, fake, p, retry_rate_limit=True) == first
    from datetime import datetime, timedelta

    monkeypatch.setattr(
        "katcha.editorial.source_intelligence.jobs._now",
        lambda: datetime.fromisoformat(first.finished_at) + timedelta(seconds=31),
    )
    second = dispatch(ledger, j, fake, p, retry_rate_limit=True)
    assert second.attempt == 2
    assert dispatch(ledger, j, fake, p, retry_rate_limit=True) == second
    assert len(fake.calls) == 2
    assert [r.status for r in ledger.history(j.job_id, scope=SCOPE)] == [
        "running",
        "rate_limited",
        "running",
        "rate_limited",
    ]


def test_worker_interruption_preserves_reservation_and_requires_reconciliation(tmp_path):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    p = policy(max_calls=1)
    j = job(p=p)
    receipt, execute = ledger._claim(j, p, CAP, retry=False)
    assert execute and receipt.status == "running"
    fake = FakeAdapter()
    assert dispatch(ledger, j, fake, p).status == "running"
    assert ledger.mark_interrupted(j.job_id, scope=SCOPE).status == "unknown"
    assert dispatch(ledger, j, fake, p, retry_rate_limit=True).status == "unknown"
    assert dispatch(ledger, job("other", p=p), fake, p).status == "quota_exceeded"
    assert not fake.calls


def test_scope_policy_identity_and_adapter_mismatches_do_not_dispatch(tmp_path):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    fake = FakeAdapter()
    wrong_scope = SCOPE.model_copy(update={"channel_id": UUID(int=3)})
    with pytest.raises(PermissionError):
        ledger.dispatch(job(), scope=wrong_scope, policy=policy(), adapter=fake, capability=CAP)
    with pytest.raises(ValueError, match="identity"):
        dispatch(ledger, job().model_copy(update={"query_terms": ("changed",)}), fake)
    assert dispatch(ledger, job(), fake).status == "succeeded"
    with pytest.raises(PermissionError):
        ledger.read(job().job_id, scope=wrong_scope)
    changed = policy(max_calls=20)
    assert dispatch(ledger, job("other", p=changed), fake, changed).status == "policy_blocked"
    assert len(fake.calls) == 1


def test_external_and_paid_legacy_adapters_are_blocked_before_network(tmp_path):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    for provider, p in [
        ("youtube", policy()),
        ("web_scout", policy(external_search_authorized=True)),
    ]:
        planned = plan_job(intent(), provider_key=provider, provider_version="v1", policy=p)
        receipt = ledger.dispatch(planned, scope=SCOPE, policy=p)
        assert receipt.status == "policy_blocked"


def test_unknown_provider_is_a_durable_policy_gap(tmp_path):
    cap = replace(CAP, provider_key="not_installed")
    planned = plan_job(
        intent(),
        provider_key=cap.provider_key,
        provider_version="v1",
        policy=policy(),
        max_results=2,
        capability=cap,
    )
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    receipt = ledger.dispatch(planned, scope=SCOPE, policy=policy())
    assert receipt.status == "policy_blocked"
    assert ledger.read(planned.job_id, scope=SCOPE) == receipt


@pytest.mark.parametrize(
    "batch",
    [
        DiscoveryBatch(items=tuple(DiscoveredCandidate("https://example.org/a") for _ in range(3))),
        DiscoveryBatch(items=(DiscoveredCandidate("file:///local/secret"),)),
        DiscoveryBatch(items=(DiscoveredCandidate("https://user:secret@example.org/a"),)),
        DiscoveryBatch(items=(), next_cursor={"page": "x" * 5000}, done=False),
        DiscoveryBatch(items=(), next_cursor={}, done=False),
        DiscoveryBatch(items=(), provider_usage={"calls": -1}),
    ],
)
def test_malformed_provider_responses_are_unknown_with_no_retry(tmp_path, batch):
    fake = FakeAdapter(batch)
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    receipt = dispatch(ledger, job(), fake)
    assert receipt.status == "unknown"
    assert dispatch(ledger, job(), fake) == receipt
    assert len(fake.calls) == 1


def test_pagination_carries_actual_cursor_and_replay_does_not_reissue(tmp_path):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    fake = FakeAdapter(DiscoveryBatch(items=(), next_cursor={"page": "two"}, done=False))
    j = job()
    receipt = dispatch(ledger, j, fake)
    page = next_page(j, receipt.next_cursor)
    fake.outcome = DiscoveryBatch(items=(DiscoveredCandidate("https://example.org/page-two"),))
    assert dispatch(ledger, page, fake).status == "succeeded"
    assert fake.calls[1][1] == {"page": "two"}
    dispatch(ledger, page, fake)
    assert len(fake.calls) == 2


def test_long_rate_limit_delay_is_not_shortened(tmp_path):
    ledger = SearchJobLedger(tmp_path / "search.sqlite")
    p = policy(max_attempts=3)
    fake = FakeAdapter(
        DiscoveryProviderError(
            "too long",
            kind="rate_limited",
            status_code=429,
            retry_after_seconds=86401,
        )
    )
    receipt = dispatch(ledger, job(p=p), fake, p)
    assert receipt.status == "failed"
    assert receipt.error_category == "retry_delay_exceeds_policy"
    assert dispatch(ledger, job(p=p), fake, p, retry_rate_limit=True) == receipt
    assert len(fake.calls) == 1
