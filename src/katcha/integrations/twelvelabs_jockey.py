"""Opt-in, read-only TwelveLabs Jockey candidate retrieval.

A Jockey result is an *unverified suggestion*. Katcha's canonical Clip,
source-relative PTS, rights policy, durable provider receipts, and human locks
remain authoritative. This module never uploads media, creates stores or
publishes/edits a storyboard.
"""

from __future__ import annotations

import importlib.util
import json
import math
import uuid
from collections.abc import Mapping
from typing import Any

from katcha.config import Settings, get_settings


class JockeyBlocked(RuntimeError):
    """Provider not admitted or the caller lacks a scoped, inspected input."""


class JockeyUnavailable(RuntimeError):
    """The provider request outcome is uncertain; never automatically replay it."""


def readiness(settings: Settings | None = None) -> dict[str, Any]:
    """Local-only diagnostics; never transmit a token or enumerate remote assets."""
    settings = settings or get_settings()
    token = settings.twelvelabs_jockey_api_token
    return {
        "sdk_installed": importlib.util.find_spec("twelvelabs") is not None,
        "token_configured": bool(token and token.get_secret_value().strip()),
        "dispatch_enabled": settings.twelvelabs_jockey_dispatch_enabled,
        "channel_store_count": len(settings.twelvelabs_jockey_knowledge_stores),
        "live_provider_checked": False,
    }


def search_candidates(
    *,
    channel_id: uuid.UUID,
    query: str,
    # Obtained by the caller from Katcha's CURRENT, rights-inspected Bin/Clip
    # records. Provider item ID -> canonical Katcha Clip UUID.
    allowed_items: Mapping[str, uuid.UUID],
    external_inspection_authorized: bool,
    settings: Settings | None = None,
    client: Any = None,
    max_results: int = 8,
) -> dict[str, Any]:
    """Return bounded provider timestamps for independent Katcha verification.

    The caller must have a durable dispatch/cost receipt and must reconcile
    uncertain outcomes before any retry. This adapter deliberately does not
    initiate or repeat indexing, billing reservations or provider submissions.
    """
    settings = settings or get_settings()
    if not external_inspection_authorized:
        raise JockeyBlocked("External footage inspection requires explicit authorization")
    if not settings.twelvelabs_jockey_dispatch_enabled:
        raise JockeyBlocked("Jockey dispatch is disabled until operator admission")
    store_id = settings.twelvelabs_jockey_knowledge_stores.get(str(channel_id))
    if not store_id:
        raise JockeyBlocked("No channel-isolated Jockey knowledge store is configured")
    token = settings.twelvelabs_jockey_api_token
    if token is None or not token.get_secret_value().strip():
        raise JockeyBlocked("Jockey API token is not available to this worker")
    if not 1 <= len(query.strip()) <= 2000:
        raise JockeyBlocked("Jockey query must contain 1 to 2000 characters")
    if not 1 <= max_results <= 32:
        raise JockeyBlocked("Jockey result limit must be between 1 and 32")
    if not 1 <= len(allowed_items) <= 500:
        raise JockeyBlocked("An explicit bounded set of permitted provider items is required")
    if any(
        not isinstance(key, str)
        or not key.strip()
        or len(key) > 128
        or not isinstance(clip_id, uuid.UUID)
        for key, clip_id in allowed_items.items()
    ):
        raise JockeyBlocked("Provider items must map to verified canonical Clip identities")

    if client is None:
        try:
            from twelvelabs import TwelveLabs
        except ImportError as exc:
            raise JockeyBlocked("Install Katcha's optional twelvelabs SDK extra") from exc
        client = TwelveLabs(api_key=token.get_secret_value())

    try:
        response = client.knowledge_stores.search(
            knowledge_store_id=store_id,
            query={"text": query.strip()},
            search_options={"video": {"modalities": ["visual", "audio"]}},
        )
    except Exception:
        # Provider SDK exceptions can contain request details; never surface them
        # (or a token) to users. Outcome may be charged even on a timeout.
        raise JockeyUnavailable(
            "Jockey search outcome is unknown; inspect provider usage before retry"
        ) from None

    suggestions: list[dict[str, Any]] = []
    ignored = 0
    for hit in response.data or []:
        if getattr(hit, "asset_type", None) != "video":
            ignored += 1
            continue
        item_id = getattr(hit, "item_id", None)
        if item_id not in allowed_items:
            ignored += 1
            continue
        rank = getattr(hit, "rank", None)
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            ignored += 1
            continue
        for interval in getattr(hit, "matches", None) or []:
            start = getattr(interval, "start_sec", None)
            end = getattr(interval, "end_sec", None)
            if (
                not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or isinstance(start, bool)
                or isinstance(end, bool)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
            ):
                ignored += 1
                continue
            suggestions.append(
                {
                    "clip_id": str(allowed_items[item_id]),
                    "provider_item_id": item_id,
                    "provider_rank": rank,
                    "source_in_seconds_unverified": float(start),
                    "source_out_seconds_unverified": float(end),
                    "verified_fact": False,
                    "production_eligible": False,
                    "rights_granted": False,
                    "human_review_required": True,
                }
            )
            if len(suggestions) >= max_results:
                break
        if len(suggestions) >= max_results:
            break

    return {
        "schema_version": "JockeyCandidateSuggestionsV1",
        "provider": "twelvelabs_jockey",
        "channel_id": str(channel_id),
        "knowledge_store_id": store_id,
        "coverage": "provider_candidate_intervals_unverified",
        "suggestions": suggestions,
        "ignored_unmapped_or_invalid": ignored,
        "human_review_required": True,
        "production_eligible": False,
        "rights_granted": False,
        "receipt_status": "caller_must_persist",
    }


if __name__ == "__main__":
    # Readiness check only. No network call, asset transfer or API charge.
    print(json.dumps(readiness(), sort_keys=True))
