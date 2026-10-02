from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy import select
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.config import get_settings
from katcha.db import session_scope
from katcha.editorial_models import EditorialRun
from katcha.orchestration.editorial_workflows import EditorialProjectWorkflow
from katcha.services.editorial_runs import ACTIVE, EditorialStopped, checkpoint, workflow_id

logger = logging.getLogger(__name__)


async def dispatch_editorial_run(client: Client, row: EditorialRun) -> str:
    identity = workflow_id(row)
    if row.status not in ACTIVE:
        return identity
    try:
        await client.start_workflow(
            EditorialProjectWorkflow.run,
            args=[str(row.id), row.attempt],
            id=identity,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            task_queue=get_settings().temporal_longform_task_queue,
        )
    except WorkflowAlreadyStartedError:
        description = await client.get_workflow_handle(identity).describe()
        if description.status != WorkflowExecutionStatus.RUNNING:
            # A dead workflow with active DB state is visible recovery work, not a new call.
            with contextlib.suppress(EditorialStopped):
                checkpoint(
                    str(row.id),
                    row.attempt,
                    status="failed",
                    error=(
                        "The workflow ended before its final checkpoint. "
                        "Resume to reuse saved work."
                    ),
                )
    return identity


async def reconcile_editorial_runs(client: Client) -> int:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(EditorialRun)
                .where(EditorialRun.status.in_(ACTIVE))
                .order_by(EditorialRun.updated_at, EditorialRun.id)
            )
        )
    dispatched = 0
    for row in rows:
        try:
            await dispatch_editorial_run(client, row)
            dispatched += 1
        except Exception:
            logger.exception("Editorial dispatch deferred for run %s", row.id)
    return dispatched


async def editorial_reconciler(client: Client) -> None:
    while True:
        try:
            await reconcile_editorial_runs(client)
        except Exception:
            logger.exception("Editorial reconciliation temporarily unavailable")
        await asyncio.sleep(30)
