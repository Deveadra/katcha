from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.config import get_settings
from katcha.intelligence.runtime import (
    DEFAULT_REFRESH_INTERVAL_HOURS,
    INTELLIGENCE_TASK_QUEUE,
)
from katcha.orchestration.analysis_workflows import ClipAnalysisWorkflow
from katcha.orchestration.intelligence_workflows import (
    ChannelIntelligenceRefreshWorkflow,
    ChannelIntelligenceScheduleWorkflow,
)
from katcha.orchestration.longform_workflows import LongformCompilationWorkflow
from katcha.orchestration.production_workflows import ShortProductionWorkflow
from katcha.orchestration.publishing_workflows import (
    YouTubeAnalyticsRefreshWorkflow,
    YouTubePublicationWorkflow,
)
from katcha.orchestration.trend_workflows import ChannelTrendRefreshWorkflow
from katcha.orchestration.workflows import ClipIngestWorkflow
from katcha.trends.runtime import TREND_TASK_QUEUE

_client: Client | None = None
_client_lock = asyncio.Lock()


async def get_temporal_client() -> Client:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            settings = get_settings()
            _client = await Client.connect(
                settings.temporal_host,
                namespace=settings.temporal_namespace,
            )
    return _client


async def start_ingest_workflow(source_id: str, workflow_id: str) -> str:
    settings = get_settings()
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            ClipIngestWorkflow.run,
            source_id,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_analysis_workflow(run_id: str, workflow_id: str) -> str:
    settings = get_settings()
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            ClipAnalysisWorkflow.run,
            args=[run_id, settings.ai_enabled],
            id=workflow_id,
            task_queue=settings.temporal_analysis_task_queue,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_production_workflow(
    production_id: str,
    workflow_id: str,
    *,
    start_stage: str = "script",
) -> str:
    settings = get_settings()
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            ShortProductionWorkflow.run,
            args=[production_id, start_stage],
            id=workflow_id,
            task_queue=settings.temporal_production_task_queue,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_longform_workflow(
    compilation_id: str,
    workflow_id: str,
    *,
    start_stage: str = "select",
) -> str:
    settings = get_settings()
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            LongformCompilationWorkflow.run,
            args=[compilation_id, start_stage],
            id=workflow_id,
            task_queue=settings.temporal_longform_task_queue,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_publication_workflow(publication_id: str, workflow_id: str) -> str:
    settings = get_settings()
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            YouTubePublicationWorkflow.run,
            args=[
                publication_id,
                settings.youtube_processing_poll_seconds,
                settings.youtube_processing_max_polls,
                settings.analytics_offsets_hours(),
            ],
            id=workflow_id,
            task_queue=settings.temporal_publishing_task_queue,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_analytics_refresh_workflow(
    publication_id: str,
    workflow_id: str,
    sample_key: str,
) -> str:
    settings = get_settings()
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            YouTubeAnalyticsRefreshWorkflow.run,
            args=[publication_id, sample_key],
            id=workflow_id,
            task_queue=settings.temporal_publishing_task_queue,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_channel_intelligence_refresh(
    channel_profile_id: str,
    workflow_id: str,
    run_key: str,
) -> str:
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            ChannelIntelligenceRefreshWorkflow.run,
            args=[channel_profile_id, run_key],
            id=workflow_id,
            task_queue=INTELLIGENCE_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_channel_intelligence_schedule(
    channel_profile_id: str,
    workflow_id: str,
    *,
    interval_hours: int = DEFAULT_REFRESH_INTERVAL_HOURS,
) -> str:
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            ChannelIntelligenceScheduleWorkflow.run,
            args=[channel_profile_id, interval_hours, 120],
            id=workflow_id,
            task_queue=INTELLIGENCE_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id


async def start_trend_refresh_workflow(
    channel_profile_id: str,
    workflow_id: str,
    run_key: str,
) -> str:
    client = await get_temporal_client()
    try:
        handle = await client.start_workflow(
            ChannelTrendRefreshWorkflow.run,
            args=[channel_profile_id, run_key],
            id=workflow_id,
            task_queue=TREND_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return handle.id
