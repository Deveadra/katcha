"""Read-only checks of the services that actually execute Katcha's workflows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, text
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE
from katcha.ai.subscription import subscription_connected
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.intelligence.runtime import INTELLIGENCE_TASK_QUEUE
from katcha.models import DomainEvent
from katcha.orchestration.client import get_temporal_client
from katcha.services.research import RESEARCH_WORKFLOW_ID
from katcha.trends.runtime import TREND_TASK_QUEUE


def _database_check() -> dict:
    with session_scope() as session:
        session.execute(text("SELECT 1"))
        latest = session.scalar(
            select(DomainEvent)
            .where(
                DomainEvent.event_type == "research.scheduler_tick",
                DomainEvent.aggregate_id == RESEARCH_WORKFLOW_ID,
            )
            .order_by(DomainEvent.created_at.desc())
            .limit(1)
        )
        return {"research_last_checked_at": latest.created_at.isoformat() if latest else None}


async def system_check() -> dict[str, object]:
    settings = get_settings()

    async def check(key: str, label: str, operation, recovery: str) -> dict:
        try:
            detail = await asyncio.wait_for(operation(), timeout=8)
            return {"key": key, "label": label, "status": "ok", "detail": detail}
        except Exception as exc:
            return {
                "key": key,
                "label": label,
                "status": "unavailable",
                "detail": f"{recovery} ({type(exc).__name__})",
            }

    async def queue_check(queue: str):
        client = await get_temporal_client()
        result = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=settings.temporal_namespace,
                task_queue=TaskQueue(name=queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            ),
            timeout=timedelta(seconds=5),
        )
        if not result.pollers:
            raise RuntimeError("No worker is polling this queue")
        return "Worker is accepting jobs"

    async def renderer_check():
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(settings.renderer_url.rstrip("/") + "/health")
            response.raise_for_status()
        return "Renderer responds"

    async def scheduler_check():
        if not settings.research_enabled:
            return "Automatic research is disabled in runtime settings"
        client = await get_temporal_client()
        description = await client.get_workflow_handle(RESEARCH_WORKFLOW_ID).describe()
        if description.status.name != "RUNNING":
            raise RuntimeError("Research dispatcher is not running")
        return "Saved sources and channel interests are scheduled"

    queues = [
        ("discovery", "Content discovery", DISCOVERY_TASK_QUEUE),
        ("trends", "Trend research", TREND_TASK_QUEUE),
        ("intelligence", "Channel intelligence", INTELLIGENCE_TASK_QUEUE),
        ("ingestion", "Video downloading", settings.temporal_task_queue),
        ("analysis", "Clip analysis", settings.temporal_analysis_task_queue),
        ("production", "Short video production", settings.temporal_production_task_queue),
        ("longform", "Long video production", settings.temporal_longform_task_queue),
        ("publishing", "Publishing and analytics", settings.temporal_publishing_task_queue),
    ]
    checks = await asyncio.gather(
        check(
            "database",
            "Saved data",
            lambda: asyncio.to_thread(_database_check),
            "Database is unavailable; open launcher diagnostics",
        ),
        check(
            "renderer",
            "Video rendering",
            renderer_check,
            "Renderer is unavailable; open launcher diagnostics",
        ),
        check(
            "research",
            "Automatic research",
            scheduler_check,
            "Restart Katcha to resume the research dispatcher",
        ),
        *(
            check(
                key,
                label,
                lambda queue=queue: queue_check(queue),
                "No responding worker; restart Katcha and inspect launcher diagnostics",
            )
            for key, label, queue in queues
        ),
    )
    ai_live = settings.ai_enabled and settings.resolved_ai_execution_mode() == "live"
    try:
        connected = await asyncio.to_thread(subscription_connected, settings)
    except Exception:
        connected = False
    configured = ai_live and bool(connected or settings.openai_api_key or settings.gemini_api_key)
    checks.append(
        {
            "key": "ai",
            "label": "AI inference",
            "status": "configured" if configured else "unavailable",
            "detail": "Provider configured. Use Test response below to verify live inference."
            if configured
            else "Select Live AI and connect a provider in Settings.",
        }
    )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "unavailable_count": sum(item["status"] == "unavailable" for item in checks),
        "live_inference_verified": False,
    }
