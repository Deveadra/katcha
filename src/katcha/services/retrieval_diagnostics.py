"""Read-only acquisition evidence, scoped independently of search configuration IDs."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from katcha.acquisition_models import DiscoveryCandidate
from katcha.content_models import ContentItem
from katcha.db import session_scope
from katcha.models import SourceItem
from katcha.orchestration.failure_details import safe_failure_text


def retrieval_capabilities() -> dict:
    from katcha.config import get_settings

    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "execution_location": "Katcha server; independent of the browser running Katcha",
        "public_page_reading": {
            "available": True,
            "tool": "read_web_page",
            "coverage": "Public HTTPS HTML/text, with timestamp and content hash",
            "limitations": [
                "No interactive JavaScript or signed-in browser sessions",
                "Page text does not verify video contents",
            ],
        },
        "media_download": {
            "engine": "yt-dlp",
            "youtube_server_session_configured": get_settings().youtube_download_cookie_file
            is not None,
            "authentication_help": "A YouTube bot/sign-in challenge requires a server "
            "download session or an authorized upload; client-browser "
            "choice does not authenticate the server.",
            "ffprobe_available": shutil.which("ffprobe") is not None,
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "live_download_verified": False,
        },
        "scene_verification": {
            "automatic_action_verification_available": False,
            "limitations": "Search metadata and sampled stills cannot confirm an action "
            "through time, episode attribution, or rights clearance.",
        },
        "discovery_configuration_tool": "discovery_adapters",
        "source_failure_tool": "source_item_status",
    }


def source_item_status(channel_id: uuid.UUID, source_item_id: uuid.UUID) -> dict:
    with session_scope() as session:
        source = session.get(SourceItem, source_item_id)
        # Do not reveal existence or error text of another channel's source.
        allowed = source is not None and (
            str((source.source_metadata or {}).get("channel_profile_id")) == str(channel_id)
            or session.scalar(
                select(ContentItem.id)
                .where(
                    ContentItem.channel_profile_id == channel_id,
                    ContentItem.source_id == source_item_id,
                )
                .limit(1)
            )
            is not None
            or session.scalar(
                select(DiscoveryCandidate.id)
                .where(
                    DiscoveryCandidate.source_item_id == source_item_id,
                    DiscoveryCandidate.candidate_metadata["channel_profile_id"].as_string()
                    == str(channel_id),
                )
                .limit(1)
            )
            is not None
        )
        if not allowed:
            raise ValueError("Media source was not found in this channel")
        return {
            "kind": "media_source",
            "source_item_id": str(source.id),
            "workflow_id": source.workflow_id,
            "status": source.status,
            "clip_id": str(source.clip_id) if source.clip_id else None,
            "error": safe_failure_text(source.error) if source.error else None,
            "diagnostic": (source.source_metadata or {}).get("ingestion_diagnostic"),
            "updated_at": source.updated_at.isoformat(),
            "scene_verified": False,
            "next_action": "Inspect workflow_status using workflow_id for execution details; "
            "source_history accepts saved search configurations only.",
        }
