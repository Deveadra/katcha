from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class DiscoveredCandidate:
    source_url: str
    external_id: str | None = None
    title: str | None = None
    creator: str | None = None
    creator_url: str | None = None
    provenance_confidence: float = 0.0
    provenance_claims: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    items: tuple[DiscoveredCandidate, ...]
    next_cursor: dict[str, Any] = field(default_factory=dict)
    done: bool = True


class DiscoveryAdapter(Protocol):
    key: str
    version: str

    def discover(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
    ) -> DiscoveryBatch: ...


class ManifestDiscoveryAdapter:
    key = "manifest"
    version = "v1"

    def discover(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
    ) -> DiscoveryBatch:
        del cursor
        raw_items = query.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("manifest discovery query.items must be a list")
        items: list[DiscoveredCandidate] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ValueError(f"manifest item {index} must be an object")
            source_url = str(raw.get("source_url") or "").strip()
            if not source_url:
                raise ValueError(f"manifest item {index} is missing source_url")
            items.append(
                DiscoveredCandidate(
                    source_url=source_url,
                    external_id=(
                        str(raw["external_id"]).strip()
                        if raw.get("external_id") is not None
                        else None
                    ),
                    title=(
                        str(raw["title"]).strip()
                        if raw.get("title") is not None
                        else None
                    ),
                    creator=(
                        str(raw["creator"]).strip()
                        if raw.get("creator") is not None
                        else None
                    ),
                    creator_url=(
                        str(raw["creator_url"]).strip()
                        if raw.get("creator_url") is not None
                        else None
                    ),
                    provenance_confidence=float(
                        raw.get("provenance_confidence") or 0.0
                    ),
                    provenance_claims=dict(raw.get("provenance_claims") or {}),
                    metadata=dict(raw.get("metadata") or {}),
                )
            )
        return DiscoveryBatch(items=tuple(items), done=True)


_ADAPTERS: dict[tuple[str, str], DiscoveryAdapter] = {}


def register_adapter(adapter: DiscoveryAdapter) -> None:
    key = (adapter.key, adapter.version)
    if key in _ADAPTERS:
        raise ValueError(
            f"discovery adapter already registered: {adapter.key}@{adapter.version}"
        )
    _ADAPTERS[key] = adapter


def get_adapter(key: str, version: str) -> DiscoveryAdapter:
    try:
        return _ADAPTERS[(key, version)]
    except KeyError as exc:
        raise ValueError(f"discovery adapter is not installed: {key}@{version}") from exc


def available_adapters() -> list[dict[str, str]]:
    return [
        {"key": key, "version": version}
        for key, version in sorted(_ADAPTERS)
    ]


register_adapter(ManifestDiscoveryAdapter())

from katcha.acquisition.feeds import RssAtomDiscoveryAdapter  # noqa: E402
from katcha.acquisition.youtube_discovery import YouTubeDiscoveryAdapter  # noqa: E402

register_adapter(RssAtomDiscoveryAdapter())
register_adapter(YouTubeDiscoveryAdapter())
