from __future__ import annotations

import hashlib

import pytest

from katcha.branding import rank_snaxx_brand_v1, rank_snaxx_brand_v2
from katcha.services.brand_assets import BUILTIN_BRAND_ASSETS, seed_builtin_brand_assets


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str | None] = {}
        self.bucket_ready = False

    def ensure_bucket(self) -> None:
        self.bucket_ready = True

    def exists(self, key: str) -> bool:
        return key in self.objects

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def put_bytes(
        self,
        data: bytes,
        key: str,
        content_type: str | None = None,
    ) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type


def test_ranksnaxx_v1_remains_immutable_and_v2_activates_reaction_pack() -> None:
    v1 = rank_snaxx_brand_v1()
    v2 = rank_snaxx_brand_v2()

    assert v1.version == 1
    assert "reaction_pack" not in v1.visual
    assert v2.version == 2
    assert v2.visual["version"] == 2
    pack = v2.visual["reaction_pack"]
    assert pack["brand_key"] == "ranksnaxx"
    assert pack["pack_key"] == "host_emotes"
    assert pack["version"] == 1
    assert pack["assets"]["meme_cry"]["storage_key"] == (
        "brands/ranksnaxx/reactions/host_emotes/v1/meme_cry.png"
    )


def test_builtin_asset_seed_is_idempotent_and_checksum_pinned() -> None:
    store = MemoryStore()

    first = seed_builtin_brand_assets(store=store)
    second = seed_builtin_brand_assets(store=store)

    assert store.bucket_ready
    assert first[0]["action"] == "seeded"
    assert second[0]["action"] == "verified"

    asset = BUILTIN_BRAND_ASSETS[0]
    data = store.objects[asset.storage_key]
    assert hashlib.sha256(data).hexdigest() == asset.sha256
    assert store.content_types[asset.storage_key] == "image/png"


def test_builtin_asset_seed_refuses_same_key_with_different_bytes() -> None:
    store = MemoryStore()
    asset = BUILTIN_BRAND_ASSETS[0]
    store.objects[asset.storage_key] = b"not-the-published-asset"

    with pytest.raises(RuntimeError, match="immutable brand asset key"):
        seed_builtin_brand_assets(store=store)
