from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from importlib.resources import files
from typing import Protocol

from katcha.integrations.storage import ObjectStore


class BrandAssetStore(Protocol):
    def ensure_bucket(self) -> None: ...
    def exists(self, key: str) -> bool: ...
    def get_bytes(self, key: str) -> bytes: ...
    def put_bytes(self, data: bytes, key: str, content_type: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class BuiltinBrandAsset:
    brand_key: str
    pack_key: str
    pack_version: int
    asset_key: str
    storage_key: str
    package_path: str
    sha256: str
    content_type: str = "image/png"

    def payload(self) -> dict[str, object]:
        return asdict(self)


_RANKSNAXX_MEME_CRY = BuiltinBrandAsset(
    brand_key="ranksnaxx",
    pack_key="host_emotes",
    pack_version=1,
    asset_key="meme_cry",
    storage_key="brands/ranksnaxx/reactions/host_emotes/v1/meme_cry.png",
    package_path="assets/ranksnaxx/reactions/host_emotes/v1/meme_cry.png",
    sha256="fc0cddb1e95245c757e01cc7363a5cdac221366598660606de5148cb966f8e2f",
)

BUILTIN_BRAND_ASSETS: tuple[BuiltinBrandAsset, ...] = (_RANKSNAXX_MEME_CRY,)


def _asset_bytes(asset: BuiltinBrandAsset) -> bytes:
    data = files("katcha").joinpath(asset.package_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != asset.sha256:
        raise RuntimeError(
            f"packaged brand asset checksum mismatch: {asset.storage_key}"
        )
    return data


def _verify_existing(
    store: BrandAssetStore,
    asset: BuiltinBrandAsset,
    expected: bytes,
) -> None:
    existing = store.get_bytes(asset.storage_key)
    digest = hashlib.sha256(existing).hexdigest()
    if digest != asset.sha256 or existing != expected:
        raise RuntimeError(
            "immutable brand asset key already exists with different bytes: "
            f"{asset.storage_key}"
        )


def seed_builtin_brand_assets(
    *,
    store: BrandAssetStore | None = None,
) -> list[dict[str, object]]:
    resolved_store = store or ObjectStore()
    resolved_store.ensure_bucket()
    results: list[dict[str, object]] = []

    for asset in BUILTIN_BRAND_ASSETS:
        data = _asset_bytes(asset)
        if resolved_store.exists(asset.storage_key):
            _verify_existing(resolved_store, asset, data)
            action = "verified"
        else:
            resolved_store.put_bytes(
                data,
                asset.storage_key,
                content_type=asset.content_type,
            )
            _verify_existing(resolved_store, asset, data)
            action = "seeded"
        results.append(
            {
                **asset.payload(),
                "action": action,
                "size_bytes": len(data),
            }
        )
    return results
