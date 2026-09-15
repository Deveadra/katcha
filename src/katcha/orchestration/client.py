from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.config import get_settings
from katcha.orchestration.analysis_workflows import ClipAnalysisWorkflow
from katcha.orchestration.workflows import ClipIngestWorkflow

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
