"""KSI-103 offline evidence; adapter observations are not media or rights proof."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch, DiscoveryProviderError
from katcha.editorial.source_intelligence.candidates import AssetCandidateV1, reference_url
from katcha.editorial.source_intelligence.contracts import (
    ProviderPolicyV1,
    SearchIntentV1,
    SearchScopeV1,
)
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability, plan_job
from katcha.editorial.source_intelligence.scouting import ScoutRequest, scout

SCOPE = SearchScopeV1(channel_id=UUID(int=1), project_id=UUID(int=2), revision_digest="a" * 64)
POLICY = ProviderPolicyV1(version="offline-v1", external_search_authorized=True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("real network prohibited")

    monkeypatch.setattr(httpx.Client, "request", reject)


class FixtureAdapter:
    version = "v1"

    def __init__(self, key, batch):
        self.key = key
        self.batch = batch
        self.calls = 0

    def discover(self, query, cursor):
        self.calls += 1
        if isinstance(self.batch, Exception):
            raise self.batch
        return self.batch


def request(key="official_fixture", batch=None, requirement="req-1"):
    cap = SearchCapability(key, "v1", "fixture", 100, external=False)
    job = plan_job(
        SearchIntentV1(
            scope=SCOPE,
            requirement_id=requirement,
            query_terms=("official trailer",),
            semantic_brief="Find reference evidence",
            source_purpose="official_reference",
            requested_by="offline-fixture",
        ),
        provider_key=key,
        provider_version="v1",
        policy=POLICY,
        max_results=3,
        capability=cap,
    )
    adapter = FixtureAdapter(key, batch or DiscoveryBatch(items=()))
    return ScoutRequest(job, POLICY, adapter, cap)


def test_multi_provider_metadata_pagination_durable_replay(tmp_path):
    official = request(
        batch=DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    "https://publisher.example/press/trailer#section",
                    external_id="press-1",
                    title="Trailer",
                    creator="Publisher",
                    creator_url="https://publisher.example/",
                    metadata={
                        "description": "Press release",
                        "download_url": "https://secret.example/file",
                        "rights_state": "production_cleared",
                        "api_key": "PRIVATE",
                    },
                ),
            ),
            next_cursor={"page": 2},
            done=False,
        )
    )
    youtube = request(
        "youtube",
        DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    "https://www.youtube.com/watch?v=video123",
                    external_id="video123",
                    title="Official trailer",
                    creator="Studio",
                    metadata={"channel_id": "UC123"},
                ),
            )
        ),
    )
    path = tmp_path / "ledger.sqlite"
    pages = scout(SearchJobLedger(path), (official, youtube), scope=SCOPE)
    assert pages[0].continuation.cursor == {"page": 2}
    for page in pages:
        candidate = page.candidates[0]
        assert candidate.availability == "reference_only"
        assert candidate.rights_state == "not_assessed"
        assert candidate.content_hash_if_obtained is None
        assert candidate.observed_metadata.verification == "provider_reported"
        assert candidate.scout_receipts == (page.receipt.receipt_id,)
        assert candidate.provenance_evidence_refs == (candidate.observed_metadata.receipt_id,)
        assert AssetCandidateV1.model_validate_json(candidate.model_dump_json()) == candidate
    assert pages[0].candidates[0].canonical_url == "https://publisher.example/press/trailer"
    assert pages[1].candidates[0].provider_media_id == "video123"
    assert "PRIVATE" not in pages[0].model_dump_json()
    assert "production_cleared" not in pages[0].model_dump_json()
    assert scout(SearchJobLedger(path), (official, youtube), scope=SCOPE) == pages
    assert official.adapter.calls == youtube.adapter.calls == 1
    second = replace(
        official,
        job=pages[0].continuation,
        adapter=FixtureAdapter(official.job.provider_key, DiscoveryBatch(items=())),
    )
    page = scout(SearchJobLedger(path), (second,), scope=SCOPE)[0]
    assert page.gap_reason == "no_matches_on_page" and page.next_action


@pytest.mark.parametrize(
    "outcome,status",
    [
        (DiscoveryProviderError("secret", kind="forbidden", status_code=403), "failed"),
        (
            DiscoveryProviderError(
                "secret", kind="rate_limited", status_code=429, retry_after_seconds=45
            ),
            "rate_limited",
        ),
        (TimeoutError("secret"), "unknown"),
    ],
)
def test_bad_provider_does_not_stop_healthy_provider_or_retry(tmp_path, outcome, status):
    bad = request("bad", outcome)
    healthy = request(
        "healthy",
        DiscoveryBatch(
            items=(
                DiscoveredCandidate(
                    "https://publisher.example/reference",
                    title="Reference",
                ),
            )
        ),
    )
    ledger = SearchJobLedger(tmp_path / "ledger.sqlite")
    pages = scout(ledger, (bad, healthy), scope=SCOPE)
    assert pages[0].receipt.status == status
    assert pages[0].gap_reason and pages[0].next_action
    assert pages[0].continuation is None and not pages[0].candidates
    assert len(pages[1].candidates) == 1
    assert "secret" not in pages[0].model_dump_json()
    assert scout(ledger, (bad, healthy), scope=SCOPE) == pages
    assert bad.adapter.calls == healthy.adapter.calls == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/private",
        "https://user:secret@public.example/a",
        "https://localhost/a",
        "https://127.0.0.1/a",
        "https://169.254.169.254/a",
        "https://[::1]/a",
        "https://example.internal/a",
        "https://public.example:999/a",
        "https://public.example/\na",
    ],
)
def test_invalid_public_references_rejected(url):
    with pytest.raises(ValueError):
        reference_url(url)


def test_duplicate_candidates_and_scope_preflight(tmp_path):
    item = DiscoveredCandidate("https://publisher.example/reference", external_id="a")
    req = request(batch=DiscoveryBatch(items=(item, item)))
    ledger = SearchJobLedger(tmp_path / "ledger.sqlite")
    other_scope = SCOPE.model_copy(update={"channel_id": UUID(int=3)})
    with pytest.raises(PermissionError):
        scout(ledger, (req,), scope=other_scope)
    assert req.adapter.calls == 0
    page = scout(ledger, (req,), scope=SCOPE)[0]
    assert len(page.candidates) == 1
    with pytest.raises(PermissionError):
        ledger.candidates(req.job.job_id, scope=other_scope)
    with pytest.raises(ValueError):
        scout(ledger, (req,) * 21, scope=SCOPE)


def test_invalid_later_item_cannot_save_partial_candidates(tmp_path):
    req = request(
        batch=DiscoveryBatch(
            items=(
                DiscoveredCandidate("https://publisher.example/valid"),
                DiscoveredCandidate("https://127.0.0.1/private"),
            )
        )
    )
    ledger = SearchJobLedger(tmp_path / "ledger.sqlite")
    page = scout(ledger, (req,), scope=SCOPE)[0]
    assert page.receipt.status == "unknown"
    assert not page.candidates
    assert scout(ledger, (req,), scope=SCOPE)[0] == page
    assert req.adapter.calls == 1


def test_installed_youtube_adapter_hydrates_metadata_offline(tmp_path, monkeypatch):
    from katcha.acquisition import youtube_discovery as module

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(youtube_data_api_key="offline-placeholder")
    )
    calls = []

    def fixture_json(client, url, **kwargs):
        calls.append(url)
        if url.endswith("/search"):
            return {"items": [{"id": {"videoId": "abc123"}}], "nextPageToken": "NEXT"}
        assert url.endswith("/videos")
        return {
            "items": [
                {
                    "id": "abc123",
                    "snippet": {
                        "title": "Hydrated official trailer",
                        "channelTitle": "Studio",
                        "channelId": "UC123",
                    },
                    "statistics": {"viewCount": "42"},
                }
            ]
        }

    monkeypatch.setattr(module, "_provider_get_json", fixture_json)
    planned = plan_job(
        SearchIntentV1(
            **{
                key: value
                for key, value in request().job.model_dump().items()
                if key in SearchIntentV1.model_fields
            }
        ),
        provider_key="youtube",
        provider_version="v1",
        policy=POLICY,
        max_results=3,
    )
    page = scout(
        SearchJobLedger(tmp_path / "ledger.sqlite"), (ScoutRequest(planned, POLICY),), scope=SCOPE
    )[0]
    assert page.receipt.status == "succeeded"
    candidate = page.candidates[0]
    assert candidate.publisher == "Studio"
    assert candidate.observed_metadata.title == "Hydrated official trailer"
    assert candidate.observed_metadata.metadata["source_metrics"]["views"] == 42
    assert page.continuation.cursor["page_token"] == "NEXT"
    assert len(calls) == 2


def test_mismatched_later_adapter_rejected_before_first_call(tmp_path):
    first = request()
    second = replace(request("second"), adapter=FixtureAdapter("wrong", DiscoveryBatch(items=())))
    with pytest.raises(ValueError, match="adapter"):
        scout(SearchJobLedger(tmp_path / "ledger.sqlite"), (first, second), scope=SCOPE)
    assert first.adapter.calls == 0


@pytest.mark.parametrize(
    "suffix",
    [
        "?api_key=PRIVATE",
        "?X-Amz-Credential=PRIVATE&X-Amz-Signature=PRIVATE",
        "?access_token=PRIVATE",
        "?sig=PRIVATE",
        "#token=PRIVATE",
    ],
)
def test_signed_or_credential_references_never_persist(tmp_path, suffix):
    req = request(
        batch=DiscoveryBatch(
            items=(DiscoveredCandidate("https://publisher.example/reference" + suffix),)
        )
    )
    ledger = SearchJobLedger(tmp_path / "ledger.sqlite")
    page = scout(ledger, (req,), scope=SCOPE)[0]
    assert page.receipt.status == "unknown" and not page.candidates
    assert not page.receipt.source_urls
    assert "PRIVATE" not in page.model_dump_json()
    assert "PRIVATE" not in str(ledger.history(req.job.job_id, scope=SCOPE))
