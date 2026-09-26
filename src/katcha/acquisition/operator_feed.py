from __future__ import annotations

from typing import Any

from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch


def _clean_string(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_list(value: object, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"operator feed {field} must be a list")
    return [cleaned for item in value if (cleaned := _clean_string(item))]


def _clean_mapping(value: object, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"operator feed {field} must be an object")
    return dict(value)


def _source_url(raw: dict[str, Any], *, index: int) -> str:
    value = _clean_string(raw.get("source_url") or raw.get("url"))
    if value is None:
        raise ValueError(f"operator feed item {index} is missing source_url")
    return value


def _confidence(raw: dict[str, Any], *, default: float) -> float:
    value = raw.get("provenance_confidence", default)
    return max(0.0, min(float(value), 1.0))


def _feed_items(query: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    raw_items = query.get("items", [])
    if raw_items is not None:
        if not isinstance(raw_items, list):
            raise ValueError("operator feed query.items must be a list")
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ValueError(f"operator feed item {index} must be an object")
            items.append(dict(raw))

    raw_urls = query.get("urls", [])
    if raw_urls is not None:
        if not isinstance(raw_urls, list):
            raise ValueError("operator feed query.urls must be a list")
        for index, url in enumerate(raw_urls):
            cleaned = _clean_string(url)
            if cleaned is None:
                raise ValueError(f"operator feed url {index} must not be blank")
            items.append({"source_url": cleaned})
    return items


class OperatorFeedDiscoveryAdapter:
    key = "operator_feed"
    version = "v1"

    def discover(
        self,
        query: dict[str, Any],
        cursor: dict[str, Any],
    ) -> DiscoveryBatch:
        del cursor
        feed_key = _clean_string(query.get("feed_key")) or "operator-feed"
        default_platform = _clean_string(query.get("default_platform"))
        default_content_kind = _clean_string(query.get("default_content_kind"))
        default_metadata = _clean_mapping(
            query.get("default_metadata"),
            field="default_metadata",
        )
        default_claims = _clean_mapping(
            query.get("default_provenance_claims"),
            field="default_provenance_claims",
        )
        raw_confidence = query.get("provenance_confidence")
        default_confidence = (
            0.65 if raw_confidence is None else float(raw_confidence)
        )
        raw_limit = query.get("limit")
        limit = int(raw_limit) if raw_limit is not None else 500
        if limit < 1:
            raise ValueError("operator feed limit must be positive")

        items: list[DiscoveredCandidate] = []
        skipped = 0
        for index, raw in enumerate(_feed_items(query), start=1):
            if raw.get("enabled") is False:
                skipped += 1
                continue
            source_url = _source_url(raw, index=index)
            platform = _clean_string(raw.get("platform")) or default_platform
            tags = _clean_list(raw.get("tags"), field="item.tags")
            metrics = _clean_mapping(raw.get("metrics"), field="item.metrics")
            item_metadata = _clean_mapping(raw.get("metadata"), field="item.metadata")
            content_kind = (
                _clean_string(raw.get("content_kind")) or default_content_kind
            )
            metadata: dict[str, Any] = {
                **default_metadata,
                "operator_feed_key": feed_key,
                "operator_feed_rank": index,
            }
            if platform:
                metadata["platform_hint"] = platform
            if content_kind:
                metadata["content_kind"] = content_kind
            if tags:
                metadata["tags"] = tags
            if metrics:
                metadata["source_metrics"] = metrics
            captured_at = _clean_string(raw.get("captured_at"))
            if captured_at:
                metadata["captured_at"] = captured_at
            original_source = _clean_string(raw.get("original_source"))
            if original_source:
                metadata["original_source"] = original_source
            clip_selector = raw.get("clip")
            if clip_selector is not None:
                metadata["clip"] = _clean_mapping(clip_selector, field="item.clip")
            metadata.update(item_metadata)

            item_claims = _clean_mapping(
                raw.get("provenance_claims"),
                field="item.provenance_claims",
            )
            provenance_claims: dict[str, Any] = {
                **default_claims,
                "discovered_via": "operator_feed",
                "operator_supplied": True,
                "feed_key": feed_key,
            }
            if platform:
                provenance_claims["platform"] = platform
            provenance_claims.update(item_claims)

            items.append(
                DiscoveredCandidate(
                    source_url=source_url,
                    external_id=_clean_string(raw.get("external_id")),
                    title=_clean_string(raw.get("title")),
                    creator=_clean_string(raw.get("creator")),
                    creator_url=_clean_string(raw.get("creator_url")),
                    provenance_confidence=_confidence(raw, default=default_confidence),
                    provenance_claims=provenance_claims,
                    metadata=metadata,
                )
            )
            if len(items) >= limit:
                break

        return DiscoveryBatch(
            items=tuple(items),
            done=True,
            provider_usage={
                "operator_feed.items": len(items),
                "operator_feed.skipped": skipped,
            },
        )
