"""Provider observations, never inferred rights or observations of media frames."""

from __future__ import annotations

import ipaddress
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import Field, JsonValue

from katcha.acquisition.adapters import DiscoveredCandidate
from katcha.editorial.source_intelligence.contracts import (
    Record,
    SearchReceiptV1,
    SearchScopeV1,
    SourceSearchJobV1,
    Text,
    canonical,
    digest,
)


def reference_url(value: str) -> str:
    """Validate a public reference without fetching it or resolving DNS.

    A future fetch must still apply its own DNS/redirect/SSRF policy.
    Preserve path/query bytes; removing parameters can change source identity.
    """
    if len(value) > 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid source reference")
    parts = urlsplit(value)
    host = parts.hostname
    if (
        parts.scheme not in {"https", "http"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or "\\" in value
        or "%" in host
        or parts.port not in {None, 80, 443}
    ):
        raise ValueError("invalid source reference")
    for key, _ in parse_qsl(parts.query) + parse_qsl(parts.fragment):
        normalized = key.casefold().replace("-", "_")
        if any(
            word in normalized
            for word in (
                "token",
                "secret",
                "password",
                "credential",
                "signature",
                "authorization",
                "api_key",
                "apikey",
                "x_amz",
                "x_goog",
            )
        ) or normalized in {"key", "sig", "policy", "key_pair_id"}:
            raise ValueError("credential or transient signed source reference")
    host = host.lower().rstrip(".")
    if "." not in host or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private source reference")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("private source reference")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, parts.query, ""))


class ProviderObservationV1(Record):
    schema_version: Literal[1] = 1
    receipt_id: str
    provider: Text
    provider_version: Text
    source_url: str
    retrieved_at: str
    title: Annotated[str, Field(max_length=2000)] | None = None
    publisher: Annotated[str, Field(max_length=500)] | None = None
    publisher_url: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    verification: Literal["provider_reported"] = "provider_reported"
    discovery_method: Annotated[str, Field(max_length=100)] | None = None


class AssetCandidateV1(Record):
    schema_version: Literal[1] = 1
    candidate_id: str
    scope: SearchScopeV1
    requirement_ids: tuple[Text, ...]
    original_url: str
    canonical_url: str
    provider: Text
    provider_version: Text
    provider_media_id: Annotated[str, Field(max_length=500)] | None = None
    publisher: Annotated[str, Field(max_length=500)] | None = None
    source_type: Literal["video_reference", "discussion_reference", "web_reference"]
    discovered_at: str
    retrieved_at: str
    observed_metadata: ProviderObservationV1
    provenance_evidence_refs: tuple[str, ...]
    scout_receipts: tuple[str, ...]
    availability: Literal["reference_only"] = "reference_only"
    rights_state: Literal["not_assessed"] = "not_assessed"
    license_evidence_refs: tuple[str, ...] = ()
    content_hash_if_obtained: None = None


# Only known informational fields cross this seam. Arbitrary adapter payloads
# can contain credentials, file/rights claims, or model-generated timestamps.
_METADATA_FIELDS = {
    "youtube_video_id",
    "channel_id",
    "published_at",
    "search_rank",
    "reddit_post_id",
    "reddit_fullname",
    "subreddit",
    "domain",
    "post_hint",
    "is_video",
    "over_18",
    "upvote_ratio",
    "source_metrics",
    "platform",
    "source_kind",
    "description",
}


def candidate_from_provider(
    item: DiscoveredCandidate, job: SourceSearchJobV1, receipt: SearchReceiptV1
) -> AssetCandidateV1:
    url = reference_url(item.source_url)
    # Preserve actual provider IDs; never construct one from a model suggestion.
    if job.provider_key == "youtube":
        from urllib.parse import parse_qs

        parts = urlsplit(url)
        if (
            parts.hostname != "www.youtube.com"
            or parts.path != "/watch"
            or parse_qs(parts.query).get("v") != [item.external_id]
        ):
            raise ValueError("YouTube URL and provider ID disagree")
    metadata = {key: value for key, value in item.metadata.items() if key in _METADATA_FIELDS}
    # Metrics are numeric observations, not an arbitrary nested payload.
    metrics = metadata.get("source_metrics")
    if metrics is not None:
        if not isinstance(metrics, dict):
            raise ValueError("provider metrics must be an object")
        metadata["source_metrics"] = {
            key: value
            for key, value in metrics.items()
            if key in {"views", "likes", "comments", "search_rank", "score", "upvotes"}
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
    if any(
        isinstance(value, (dict, list))
        for key, value in metadata.items()
        if key != "source_metrics"
    ):
        raise ValueError("unexpected nested metadata")
    if len(canonical(metadata).encode()) > 16384:
        raise ValueError("provider metadata exceeds evidence bound")
    observation = ProviderObservationV1(
        receipt_id="smo_" + digest([receipt.receipt_id, url, item.external_id]),
        provider=job.provider_key,
        provider_version=job.provider_version,
        source_url=item.source_url,
        retrieved_at=receipt.started_at,
        title=item.title,
        publisher=item.creator,
        publisher_url=reference_url(item.creator_url) if item.creator_url else None,
        metadata=metadata,
        discovery_method=item.provenance_claims.get("discovered_via"),
    )
    return AssetCandidateV1(
        candidate_id="asc_" + digest([job.scope.key, job.provider_key, url, item.external_id]),
        scope=job.scope,
        requirement_ids=(job.requirement_id,),
        original_url=item.source_url,
        canonical_url=url,
        provider=job.provider_key,
        provider_version=job.provider_version,
        provider_media_id=item.external_id,
        publisher=item.creator,
        source_type=(
            "video_reference"
            if job.provider_key == "youtube"
            else "discussion_reference"
            if job.provider_key == "reddit"
            else "web_reference"
        ),
        discovered_at=receipt.started_at,
        retrieved_at=receipt.started_at,
        observed_metadata=observation,
        provenance_evidence_refs=(observation.receipt_id,),
        scout_receipts=(receipt.receipt_id,),
    )
