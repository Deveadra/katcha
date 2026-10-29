"""Conservative read-only assessment of durable media and publication outcome records.

This checks *database evidence consistency*, not remote object existence, provider
availability, Temporal recovery, permissions, or authorization to publish/retry.
Never include raw provider payloads, object keys or exceptions in the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from katcha.production_models import Production, ProductionAsset
from katcha.publishing_models import Publication, PublicationAnalyticsSnapshot
from katcha.render_models import RenderAttempt
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset

RecordAssessment = Literal["unverified", "recorded", "corroborated", "inconsistent"]


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    assessment: RecordAssessment
    evidence_codes: tuple[str, ...]
    missing_codes: tuple[str, ...]
    # No passive database-only check establishes these facts.
    live_provider_checked: bool = False
    business_recovery_verified: bool = False


def assess_render_records(
    source: Production | ShortEpisode,
    attempt: RenderAttempt | None,
    asset: ProductionAsset | ShortEpisodeAsset | None,
) -> OutcomeRecord:
    """A verified render attempt and matching asset receipt are distinct facts."""
    evidence: list[str] = []
    missing: list[str] = []
    conflicting = False

    if attempt is None:
        missing.append("RENDER_ATTEMPT_MISSING")
    else:
        if attempt.status == "dead_letter":
            missing.append("RENDER_DEAD_LETTER")
            conflicting = asset is not None and bool((asset.asset_metadata or {}).get("verified"))
        elif attempt.status == "verified" and attempt.completed_at is not None:
            evidence.append("RENDER_ATTEMPT_VERIFIED_RECORD")
        else:
            missing.append("RENDER_ATTEMPT_NOT_VERIFIED")
        if attempt.source_generation != source.generation:
            missing.append("RENDER_ATTEMPT_GENERATION_MISMATCH")
            conflicting = True

    if asset is None:
        missing.append("RENDER_ASSET_RECEIPT_MISSING")
    elif asset.kind != "render":
        missing.append("RENDER_ASSET_RECEIPT_INVALID")
        conflicting = True
    else:
        if (asset.asset_metadata or {}).get("verified") is True:
            evidence.append("RENDER_ASSET_VERIFIED_RECORD")
        else:
            missing.append("RENDER_ASSET_UNVERIFIED")
        if not isinstance(asset.storage_key, str) or not asset.storage_key.strip():
            missing.append("RENDER_OUTPUT_KEY_MISSING")
        elif attempt is not None and asset.storage_key != attempt.output_key:
            missing.append("RENDER_OUTPUT_KEY_MISMATCH")
            conflicting = True
        elif attempt is not None:
            evidence.append("RENDER_OUTPUT_KEYS_MATCH")

    if conflicting:
        assessment: RecordAssessment = "inconsistent"
    elif (
        "RENDER_ATTEMPT_VERIFIED_RECORD" in evidence
        and "RENDER_ASSET_VERIFIED_RECORD" in evidence
        and "RENDER_OUTPUT_KEYS_MATCH" in evidence
    ):
        assessment = "corroborated"
    elif evidence:
        assessment = "recorded"
    else:
        assessment = "unverified"
    return OutcomeRecord(assessment, tuple(evidence), tuple(missing))


def _timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def assess_publication_records(
    publication: Publication,
    latest_analytics: PublicationAnalyticsSnapshot | None,
) -> OutcomeRecord:
    """Provider observations corroborate local state, but are not live provider checks."""
    evidence: list[str] = []
    missing: list[str] = []
    conflicting = False
    video_id = publication.youtube_video_id
    valid_id = isinstance(video_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{6,64}", video_id))
    published_at = _timestamp(publication.published_at)
    if valid_id:
        evidence.append("YOUTUBE_ID_RECORDED")
    else:
        missing.append("YOUTUBE_ID_MISSING_OR_INVALID")
    if published_at is not None:
        evidence.append("PUBLISHED_AT_RECORDED")
    else:
        missing.append("PUBLISHED_AT_MISSING")
    if publication.status == "published":
        evidence.append("PUBLICATION_COMPLETED_RECORD")
    else:
        missing.append("PUBLICATION_NOT_COMPLETE")
        if publication.status in {"failed", "rejected"} and (valid_id or published_at):
            conflicting = True

    if (
        latest_analytics is not None
        and valid_id
        and published_at is not None
        and _timestamp(latest_analytics.sampled_at) is not None
        and _timestamp(latest_analytics.sampled_at) >= published_at
        and latest_analytics.publication_id == publication.id
    ):
        evidence.append("POST_PUBLICATION_ANALYTICS_RECORDED")
    else:
        missing.append("POST_PUBLICATION_ANALYTICS_NOT_CONFIRMED")

    if conflicting or (
        publication.status == "published" and (not valid_id or published_at is None)
    ):
        assessment: RecordAssessment = "inconsistent"
    elif publication.status == "published" and (
        "POST_PUBLICATION_ANALYTICS_RECORDED" in evidence
    ):
        assessment = "corroborated"
    elif publication.status == "published" and valid_id and published_at is not None:
        assessment = "recorded"
    else:
        assessment = "unverified"
    return OutcomeRecord(assessment, tuple(evidence), tuple(missing))
