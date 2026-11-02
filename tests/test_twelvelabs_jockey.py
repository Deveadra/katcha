"""Offline TwelveLabs Jockey adapter contracts; no provider traffic or spend."""

from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace

from pydantic import SecretStr, ValidationError
import pytest

from katcha.config import Settings
from katcha.integrations import twelvelabs_jockey as jockey


CHANNEL = uuid.UUID("11111111-1111-4111-8111-111111111111")
CLIP = uuid.UUID("22222222-2222-4222-8222-222222222222")
ITEM = "provider-item-1"
STORE = "store_12345678"


def settings(*, enabled: bool = True, token: str | None = "fixture-token", stores=None):
    return Settings(
        _env_file=None,
        twelvelabs_jockey_api_token=SecretStr(token) if token else None,
        twelvelabs_jockey_dispatch_enabled=enabled,
        twelvelabs_jockey_knowledge_stores=(
            {str(CHANNEL): STORE} if stores is None else stores
        ),
    )


def call(*, config=None, authorized=True, client=None, **overrides):
    return jockey.search_candidates(
        channel_id=CHANNEL,
        query="goalkeeper dives left",
        allowed_items={ITEM: CLIP},
        external_inspection_authorized=authorized,
        settings=config or settings(),
        client=client,
        **overrides,
    )


class FakeClient:
    def __init__(self, data):
        self.calls = []
        self.knowledge_stores = SimpleNamespace(search=self.search)
        self.data = data

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data=self.data)


def hit(item=ITEM, *, start=1.5, end=4.0, rank=1, asset_type="video"):
    return SimpleNamespace(
        item_id=item,
        asset_type=asset_type,
        rank=rank,
        matches=[SimpleNamespace(start_sec=start, end_sec=end)],
    )


@pytest.mark.parametrize(
    ("config", "authorized", "allowed"),
    [
        (settings(enabled=False), True, {ITEM: CLIP}),
        (settings(), False, {ITEM: CLIP}),
        (settings(token=None), True, {ITEM: CLIP}),
        (settings(stores={}), True, {ITEM: CLIP}),
        (settings(), True, {}),
        (settings(), True, {ITEM: "not-a-clip-uuid"}),
    ],
)
def test_admission_blocks_before_any_sdk_request(config, authorized, allowed):
    client = FakeClient([hit()])
    with pytest.raises(jockey.JockeyBlocked):
        jockey.search_candidates(
            channel_id=CHANNEL,
            query="scene",
            allowed_items=allowed,
            external_inspection_authorized=authorized,
            settings=config,
            client=client,
        )
    assert not client.calls


def test_scoped_results_are_only_unverified_candidates():
    client = FakeClient([hit("wrong-provider-item"), hit(), hit(start=9, end=8)])
    result = call(client=client)
    assert client.calls == [
        {
            "knowledge_store_id": STORE,
            "query": {"text": "goalkeeper dives left"},
            "search_options": {"video": {"modalities": ["visual", "audio"]}},
        }
    ]
    assert result["schema_version"] == "JockeyCandidateSuggestionsV1"
    assert result["ignored_unmapped_or_invalid"] == 2
    assert result["receipt_status"] == "caller_must_persist"
    assert len(result["suggestions"]) == 1
    candidate = result["suggestions"][0]
    assert candidate["clip_id"] == str(CLIP)
    assert candidate["source_in_seconds_unverified"] == 1.5
    assert candidate["source_out_seconds_unverified"] == 4.0
    assert not candidate["verified_fact"]
    assert not candidate["rights_granted"]
    assert not candidate["production_eligible"]
    assert candidate["human_review_required"]


@pytest.mark.parametrize("start,end", [(-1, 2), (3, 2), (float("nan"), 2), (0, float("inf"))])
def test_invalid_provider_time_ranges_are_discarded(start, end):
    result = call(client=FakeClient([hit(start=start, end=end)]))
    assert result["suggestions"] == []
    assert result["ignored_unmapped_or_invalid"] == 1


def test_sdk_client_uses_secret_without_logging_or_remote_setup(monkeypatch):
    created = []
    client = FakeClient([hit()])

    def make_client(*, api_key):
        created.append(api_key)
        return client

    monkeypatch.setitem(sys.modules, "twelvelabs", SimpleNamespace(TwelveLabs=make_client))
    result = call(config=settings(token="fixture-secret-should-not-leak"))
    assert created == ["fixture-secret-should-not-leak"]
    assert result["suggestions"][0]["clip_id"] == str(CLIP)
    assert "fixture-secret" not in str(result)


def test_provider_exception_is_redacted_and_not_retried():
    class Broken:
        def search(self, **kwargs):
            raise RuntimeError("secret-token and sensitive request body")

    client = SimpleNamespace(knowledge_stores=SimpleNamespace(search=Broken().search))
    with pytest.raises(jockey.JockeyUnavailable) as error:
        call(client=client)
    assert "secret-token" not in str(error.value)
    assert "unknown" in str(error.value)


def test_channel_store_mapping_cannot_share_provider_corpus():
    other = str(uuid.UUID("33333333-3333-4333-8333-333333333333"))
    with pytest.raises(ValidationError, match="distinct"):
        settings(stores={str(CHANNEL): STORE, other: STORE})
    with pytest.raises(ValidationError, match="canonical"):
        settings(stores={str(CHANNEL).upper(): STORE})


def test_defaults_do_not_enable_paid_provider():
    config = Settings(_env_file=None)
    assert config.twelvelabs_jockey_dispatch_enabled is False
    assert config.twelvelabs_jockey_knowledge_stores == {}
