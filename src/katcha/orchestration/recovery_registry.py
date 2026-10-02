from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RecoveryMode(StrEnum):
    PERSISTED_RECONCILE = "persisted_reconcile"
    SINGLETON_RECONSTRUCT = "singleton_reconstruct"
    TEMPORAL_SCHEDULE = "temporal_schedule"
    REQUEST_SCOPED = "request_scoped"
    PARENT_OWNED = "parent_owned"


@dataclass(frozen=True, slots=True)
class WorkflowRecoveryContract:
    workflow: str
    mode: RecoveryMode
    owner: str
    rationale: str


WORKFLOW_RECOVERY_CONTRACTS: dict[str, WorkflowRecoveryContract] = {
    "ClipIngestWorkflow": WorkflowRecoveryContract(
        "ClipIngestWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "worker",
        "SourceItem persists workflow_id and active ingest states.",
    ),
    "ClipAnalysisWorkflow": WorkflowRecoveryContract(
        "ClipAnalysisWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "analysis-worker",
        "ClipAnalysisRun persists workflow_id and queued/running state.",
    ),
    "ShortProductionWorkflow": WorkflowRecoveryContract(
        "ShortProductionWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "production-worker",
        "Production persists workflow_id, status and restart stage.",
    ),
    "RankedShortEpisodeEditorialWorkflow": WorkflowRecoveryContract(
        "RankedShortEpisodeEditorialWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "production-worker",
        "ShortEpisode persists workflow identity and editorial/render stage.",
    ),
    "LongformCompilationWorkflow": WorkflowRecoveryContract(
        "LongformCompilationWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "production-worker",
        "Compilation persists workflow_id and restart stage.",
    ),
    "StagedBrandPreviewWorkflow": WorkflowRecoveryContract(
        "StagedBrandPreviewWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "production-worker",
        "BrandPreviewRender persists workflow_id and queued/rendering state.",
    ),
    "YouTubePublicationWorkflow": WorkflowRecoveryContract(
        "YouTubePublicationWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "worker",
        "Publication persists workflow_id, upload offset and processing state.",
    ),
    "YouTubePackagingActivationWorkflow": WorkflowRecoveryContract(
        "YouTubePackagingActivationWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "worker",
        "Packaging activation persists workflow_id, status and mutation stage.",
    ),
    "DiscoveryRunWorkflow": WorkflowRecoveryContract(
        "DiscoveryRunWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "intelligence-worker",
        "DiscoveryRun is reconstructed from durable resumable source runs.",
    ),
    "CommandGoalWorkflow": WorkflowRecoveryContract(
        "CommandGoalWorkflow",
        RecoveryMode.PERSISTED_RECONCILE,
        "intelligence-worker",
        "CommandGoal persists authority, step_count, observations and wait state.",
    ),
    "AutomaticResearchWorkflow": WorkflowRecoveryContract(
        "AutomaticResearchWorkflow",
        RecoveryMode.SINGLETON_RECONSTRUCT,
        "intelligence-worker",
        "Fixed workflow ID is restarted whenever research is enabled.",
    ),
    "TopicWatchScheduleWorkflow": WorkflowRecoveryContract(
        "TopicWatchScheduleWorkflow",
        RecoveryMode.TEMPORAL_SCHEDULE,
        "intelligence-worker",
        "Long-running schedule is durable in Temporal; schedule config "
        "persistence is a cutover gate.",
    ),
    "ChannelIntelligenceScheduleWorkflow": WorkflowRecoveryContract(
        "ChannelIntelligenceScheduleWorkflow",
        RecoveryMode.TEMPORAL_SCHEDULE,
        "intelligence-worker",
        "Long-running schedule is durable in Temporal; active-channel schedule "
        "reconstruction is a cutover gate.",
    ),
    "ChannelTrendActivationScheduleWorkflow": WorkflowRecoveryContract(
        "ChannelTrendActivationScheduleWorkflow",
        RecoveryMode.TEMPORAL_SCHEDULE,
        "intelligence-worker",
        "Long-running schedule is durable in Temporal; activation schedule config "
        "persistence is a cutover gate.",
    ),
    "CommandSourcePrepareWorkflow": WorkflowRecoveryContract(
        "CommandSourcePrepareWorkflow",
        RecoveryMode.PARENT_OWNED,
        "intelligence-worker",
        "Command goal/action lifecycle owns the request while child discovery/ingest "
        "work is independently durable.",
    ),
    "TopicWatchWorkflow": WorkflowRecoveryContract(
        "TopicWatchWorkflow",
        RecoveryMode.PARENT_OWNED,
        "intelligence-worker",
        "Manual/scheduled parent owns execution identity; child discovery runs are "
        "independently durable.",
    ),
    "YouTubeAnalyticsWorkflow": WorkflowRecoveryContract(
        "YouTubeAnalyticsWorkflow",
        RecoveryMode.PARENT_OWNED,
        "worker",
        "Publication workflow owns scheduled analytics children.",
    ),
    "YouTubeAnalyticsRefreshWorkflow": WorkflowRecoveryContract(
        "YouTubeAnalyticsRefreshWorkflow",
        RecoveryMode.REQUEST_SCOPED,
        "worker",
        "Idempotent sample_key request; no standalone active-state row requires replay.",
    ),
    "YouTubeReachSyncWorkflow": WorkflowRecoveryContract(
        "YouTubeReachSyncWorkflow",
        RecoveryMode.REQUEST_SCOPED,
        "worker",
        "Provider job identity is durable and recurring cadence can issue another idempotent sync.",
    ),
    "ChannelIntelligenceRefreshWorkflow": WorkflowRecoveryContract(
        "ChannelIntelligenceRefreshWorkflow",
        RecoveryMode.REQUEST_SCOPED,
        "intelligence-worker",
        "Refresh run keys make manual and schedule-triggered executions idempotent.",
    ),
    "ChannelTrendActivationWorkflow": WorkflowRecoveryContract(
        "ChannelTrendActivationWorkflow",
        RecoveryMode.REQUEST_SCOPED,
        "intelligence-worker",
        "TrendActivationRun run_key and decisions persist idempotent activation progress.",
    ),
    "ChannelTrendActivationPerformanceWorkflow": WorkflowRecoveryContract(
        "ChannelTrendActivationPerformanceWorkflow",
        RecoveryMode.REQUEST_SCOPED,
        "intelligence-worker",
        "Performance refresh is keyed and recomputable from durable observations.",
    ),
    "ChannelTrendRefreshWorkflow": WorkflowRecoveryContract(
        "ChannelTrendRefreshWorkflow",
        RecoveryMode.REQUEST_SCOPED,
        "intelligence-worker",
        "Trend refresh uses a durable run_key and can be safely requested again.",
    ),
    "ChannelTrendCalibrationWorkflow": WorkflowRecoveryContract(
        "ChannelTrendCalibrationWorkflow",
        RecoveryMode.REQUEST_SCOPED,
        "intelligence-worker",
        "Calibration is keyed and recomputable from durable outcomes.",
    ),
}


def recovery_contract(workflow_name: str) -> WorkflowRecoveryContract:
    try:
        return WORKFLOW_RECOVERY_CONTRACTS[workflow_name]
    except KeyError as exc:
        raise KeyError(f"workflow has no recovery contract: {workflow_name}") from exc
