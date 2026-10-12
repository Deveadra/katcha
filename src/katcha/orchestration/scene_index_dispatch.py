"""Recover durable index queues on the existing Editorial Temporal worker."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.editorial.media_intake.scene_index import (
    index_project_media,
    stop_scene_index_attempt,
)
from katcha.orchestration.scene_index_workflows import SceneIndexWorkflow
from katcha.scene_index_models import MediaSceneIndex

logger = logging.getLogger(__name__)


@activity.defn
def measure_project_media(context: dict[str, str]) -> None:
    index_project_media(
        uuid.UUID(context["channel_id"]),
        uuid.UUID(context["project_id"]),
        uuid.UUID(context["item_id"]),
        actor=context["actor"],
        lease_token=context["token"],
    )


@activity.defn
def fail_scene_index_attempt(context: dict[str, str]) -> None:
    stop_scene_index_attempt(
        uuid.UUID(context["index_id"]), context["token"], "worker_interrupted_or_failed"
    )


async def reconcile_scene_indexes(client: Client) -> int:
    with session_scope() as session:
        rows = list(
            session.scalars(select(MediaSceneIndex).where(MediaSceneIndex.status == "indexing"))
        )
    dispatched = 0
    for row in rows:
        receipt = row.receipt
        if receipt.get("dispatch") not in {"queued", "running"}:
            continue  # Legacy synchronous attempts require operator reconciliation.
        context = {
            "index_id": str(row.id),
            "token": receipt["lease_token"],
            "channel_id": str(row.channel_profile_id),
            "project_id": receipt["project_id"],
            "item_id": receipt["item_id"],
            "actor": receipt["actor"],
        }
        identity = f"scene-index:{row.id}:{context['token']}"
        try:
            await client.start_workflow(
                SceneIndexWorkflow.run,
                context,
                id=identity,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                task_queue=get_settings().temporal_longform_task_queue,
            )
            dispatched += 1
        except WorkflowAlreadyStartedError:
            description = await client.get_workflow_handle(identity).describe()
            if description.status != WorkflowExecutionStatus.RUNNING:
                fail_scene_index_attempt(context)
        except Exception:
            logger.exception("Measured index dispatch deferred for %s", row.id)
    return dispatched
