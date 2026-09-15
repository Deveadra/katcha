from __future__ import annotations

from enum import StrEnum


class SourceStatus(StrEnum):
    REGISTERED = "registered"
    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ProductionStatus(StrEnum):
    QUEUED = "queued"
    SCRIPTING = "scripting"
    SCRIPTED = "scripted"
    VOICING = "voicing"
    VOICED = "voiced"
    RENDERING = "rendering"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class CompilationStatus(StrEnum):
    QUEUED = "queued"
    SELECTING = "selecting"
    PLANNING = "planning"
    CRITIQUING = "critiquing"
    SCRIPTED = "scripted"
    VOICING = "voicing"
    VOICED = "voiced"
    RENDERING = "rendering"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REGENERATE = "regenerate"


class YouTubeConnectionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class PublicationStatus(StrEnum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PRIVATE = "private"
    UNLISTED = "unlisted"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class ClipStatus(StrEnum):
    INGESTED = "ingested"
    NORMALIZED = "normalized"
    ANALYZED = "analyzed"
    SCORED = "scored"
    SELECTED = "selected"
    SCRIPTED = "scripted"
    VOICED = "voiced"
    RENDERED = "rendered"
    REVIEW = "review"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    MEASURED = "measured"
    FAILED = "failed"


CLIP_TRANSITIONS: dict[ClipStatus, set[ClipStatus]] = {
    ClipStatus.INGESTED: {ClipStatus.NORMALIZED, ClipStatus.FAILED},
    ClipStatus.NORMALIZED: {ClipStatus.ANALYZED, ClipStatus.FAILED},
    ClipStatus.ANALYZED: {ClipStatus.SCORED, ClipStatus.FAILED},
    ClipStatus.SCORED: {ClipStatus.SELECTED, ClipStatus.FAILED},
    ClipStatus.SELECTED: {ClipStatus.SCRIPTED, ClipStatus.FAILED},
    ClipStatus.SCRIPTED: {ClipStatus.VOICED, ClipStatus.FAILED},
    ClipStatus.VOICED: {ClipStatus.RENDERED, ClipStatus.FAILED},
    ClipStatus.RENDERED: {ClipStatus.REVIEW, ClipStatus.FAILED},
    ClipStatus.REVIEW: {ClipStatus.SCHEDULED, ClipStatus.FAILED},
    ClipStatus.SCHEDULED: {ClipStatus.PUBLISHED, ClipStatus.FAILED},
    ClipStatus.PUBLISHED: {ClipStatus.MEASURED, ClipStatus.FAILED},
    ClipStatus.MEASURED: set(),
    ClipStatus.FAILED: set(),
}


def require_clip_transition(current: ClipStatus, target: ClipStatus) -> None:
    if target not in CLIP_TRANSITIONS[current]:
        raise ValueError(f"invalid clip transition: {current} -> {target}")


class AITask(StrEnum):
    BULK_VISION = "bulk_vision"
    DEEP_VIDEO = "deep_video"
    SHORT_SCRIPT = "short_script"
    LONGFORM_EDITOR = "longform_editor"
    LONGFORM_CRITIC = "longform_critic"
    METADATA = "metadata"
    PERFORMANCE_ANALYSIS = "performance_analysis"
    TTS = "tts"
