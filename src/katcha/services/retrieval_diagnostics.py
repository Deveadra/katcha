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
    from katcha.editorial.media_intake.scene_index import MAX_INDEX_BYTES, MAX_SEARCH_BYTES
    from katcha.editorial.media_intake.scene_worker import MAX_FRAMES, MAX_SECONDS
    from katcha.editorial.semantic_schemas import SemanticMatchRequest
    from katcha.services.browser_client import browser_configuration

    settings = get_settings()
    semantic_schema = SemanticMatchRequest.model_json_schema()["properties"]
    return {
        "interactive_browser": browser_configuration(),
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
            "youtube_download_route_configured": get_settings().youtube_download_proxy_url
            is not None,
            "youtube_server_session_configured": get_settings().youtube_download_cookie_file
            is not None,
            "authentication_help": "Public videos normally need no sign-in. YouTube can "
            "still challenge the server request. An authorized server session may help "
            "but cannot guarantee access; try another accessible approved source. "
            "Client-browser sign-in does not authenticate the worker.",
            "ffprobe_available": shutil.which("ffprobe") is not None,
            "ffmpeg_available": shutil.which("ffmpeg") is not None,
            "dependency_check_location": "API/goal process, not a probe of the ingestion worker",
            "live_download_verified": False,
            "live_download_verification_state": "not_tested",
            "live_download_note": (
                "No external media transfer is performed by this capability check"
            ),
        },
        "scene_verification": {
            "automatic_action_verification_available": False,
            "limitations": "Search metadata and sampled stills cannot confirm an action "
            "through time, episode attribution, or rights clearance.",
        },
        "measured_scene_index": {
            "schema_version": "SceneIndexV1",
            "max_input_bytes": MAX_INDEX_BYTES,
            "max_search_bytes": MAX_SEARCH_BYTES,
            "max_duration_seconds": MAX_SECONDS,
            "max_decoded_frames": MAX_FRAMES,
            "coverage": "all_decoded_frames_visual_change_sparse_keyframes",
            "semantic_actions_verified": False,
            "live_index_verified": False,
            "next_action": "Use the existing rights-gated project media index. Larger or "
            "longer media requires bounded windowed analysis, not higher host limits.",
        },
        "optional_semantic_inspection": {
            "status": "unknown" if settings.semantic_matching_dispatch_enabled else "not_enabled",
            "dispatch_enabled": settings.semantic_matching_dispatch_enabled,
            "max_requirements": semantic_schema["requirement_ids"]["maxItems"],
            "max_shots": semantic_schema["shot_ids"]["maxItems"],
            "evidence_kind": "shot_start_frame_hypotheses",
            "external_authorization_required": True,
            "automatic_selection_available": False,
            "live_provider_verified": False,
            "next_action": "Verify channel inspection permission, provider and budget admission; "
            "enabled dispatch alone does not prove inference or temporal understanding.",
        },
        "remote_acquisition": {
            "configured_max_bytes": settings.remote_acquisition_max_bytes,
            "live_transfer_verified": False,
            "analysis_capacity_is_independent": True,
        },
        "discovery_configuration_tool": "discovery_adapters",
        "source_failure_tool": "source_item_status",
    }


async def retrieval_capabilities_checked() -> dict:
    """Combine static capabilities with live browser readiness, never a provider claim."""
    from katcha.services.browser_client import probe_browser_service

    result = retrieval_capabilities()
    result["interactive_browser"] = await probe_browser_service()
    return result


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
