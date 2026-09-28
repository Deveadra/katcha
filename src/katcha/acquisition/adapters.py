from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class DiscoveryProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "provider_error",
        transient: bool = True,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        provider_usage: dict[str, int] | None = None,
    ) -> None:
        details: list[str] = []
        if status_code is not None:
            details.append(f"status {status_code}")
        if retry_after_seconds is not None:
            details.append(f"retry_after_seconds={retry_after_seconds}")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"{message}{suffix}")
        self.kind = kind
        self.transient = transient
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_usage = {
            str(key): max(int(value), 0)
            for key, value in (provider_usage or {}).items()
        }


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
    provider_usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveryAdapterCapability:
    key: str
    version: str
    label: str
    description: str
    source_types: tuple[str, ...] = ()
    supported_platforms: tuple[str, ...] = ()
    query_fields: tuple[str, ...] = ()
    required_credentials: tuple[str, ...] = ()
    supports_imports: bool = False
    sample_query: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "label": self.label,
            "description": self.description,
            "source_types": list(self.source_types),
            "supported_platforms": list(self.supported_platforms),
            "query_fields": list(self.query_fields),
            "required_credentials": list(self.required_credentials),
            "supports_imports": self.supports_imports,
            "sample_query": dict(self.sample_query),
        }


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
    capability = DiscoveryAdapterCapability(
        key=key,
        version=version,
        label="Manifest",
        description=(
            "Accepts already-known candidate URLs and metadata from operators, "
            "scripts, backfills, or trusted internal systems."
        ),
        source_types=("operator_drop", "server_drop", "backfill"),
        supported_platforms=("any",),
        query_fields=("items",),
        sample_query={
            "items": [
                {
                    "source_url": "https://example.com/video/1",
                    "external_id": "example-1",
                    "metadata": {"content_lane": "candidate_review"},
                }
            ]
        },
    )

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


def describe_adapter(adapter: DiscoveryAdapter) -> DiscoveryAdapterCapability:
    capability = getattr(adapter, "capability", None)
    if isinstance(capability, DiscoveryAdapterCapability):
        return capability
    return DiscoveryAdapterCapability(
        key=adapter.key,
        version=adapter.version,
        label=adapter.key.replace("_", " ").title(),
        description="Installed discovery adapter.",
    )


def available_adapters() -> list[dict[str, Any]]:
    return [
        describe_adapter(_ADAPTERS[(key, version)]).as_dict()
        for key, version in sorted(_ADAPTERS)
    ]


register_adapter(ManifestDiscoveryAdapter())

from katcha.acquisition.feeds import RssAtomDiscoveryAdapter  # noqa: E402
from katcha.acquisition.operator_feed import OperatorFeedDiscoveryAdapter  # noqa: E402
from katcha.acquisition.reddit_discovery import RedditDiscoveryAdapter  # noqa: E402
from katcha.acquisition.web_scout import WebScoutDiscoveryAdapter  # noqa: E402
from katcha.acquisition.youtube_discovery import YouTubeDiscoveryAdapter  # noqa: E402

register_adapter(OperatorFeedDiscoveryAdapter())
register_adapter(RedditDiscoveryAdapter())
register_adapter(RssAtomDiscoveryAdapter())
register_adapter(WebScoutDiscoveryAdapter())
register_adapter(YouTubeDiscoveryAdapter())
