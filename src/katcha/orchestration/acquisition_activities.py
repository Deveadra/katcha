"""Primary-side recovery and validation. These activities never acquire from providers."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from datetime import timedelta

from sqlalchemy import case, select
from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from katcha.acquisition.job_contracts import EnqueueJob
from katcha.acquisition_job_models import AcquisitionJob
from katcha.acquisition_models import DiscoveryCandidate
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.editorial_models import AcquisitionReceipt
from katcha.models import SourceItem
from katcha.services import acquisition_jobs as jobs


@activity.defn
def remote_source_acquisition(source_id: str) -> dict:
    from temporalio.exceptions import ApplicationError

    settings = get_settings()
    with session_scope() as session:
        source = session.get(SourceItem, uuid.UUID(source_id))
        if source is None:
            raise ValueError("Source not found")
        if source.status == "ready" and source.clip_id is not None:
            return {}
        metadata = source.source_metadata or {}
        job_id = metadata.get("remote_acquisition_job_id")
        channel = metadata.get("channel_profile_id")
        if job_id:
            row = jobs._job(session, uuid.UUID(channel), uuid.UUID(job_id))
            if row.source_item_id != source.id:
                raise ValueError("Remote acquisition source linkage does not match")
            return {"job_id": str(row.id), "channel_id": str(row.channel_profile_id)}
        if not (
            settings.remote_acquisition_enabled
            and settings.remote_acquisition_auto_dispatch_enabled
        ):
            return {}
        candidate = session.scalar(
            select(DiscoveryCandidate).where(DiscoveryCandidate.source_item_id == source.id)
        )
        receipt = (
            session.scalar(
                select(AcquisitionReceipt)
                .where(
                    AcquisitionReceipt.candidate_id == candidate.id,
                    AcquisitionReceipt.channel_profile_id == uuid.UUID(channel),
                    AcquisitionReceipt.status == "authorized",
                )
                .order_by(AcquisitionReceipt.created_at.desc())
            )
            if candidate and channel
            else None
        )
        if receipt is None:
            raise ApplicationError(
                "Approve a channel-owned source download permission "
                "before remote acquisition can start",
                type="RemoteAcquisitionAuthorizationRequired",
                non_retryable=True,
            )
        receipt_id, channel_id = receipt.id, receipt.channel_profile_id
    row = jobs.enqueue_job(
        channel_id,
        EnqueueJob(receipt_id=receipt_id, max_bytes=settings.remote_acquisition_max_bytes),
        "ingest-workflow",
    )
    return {"job_id": str(row.id), "channel_id": str(channel_id)}


@activity.defn
def reconcile_remote_acquisition(channel_id: str, job_id: str) -> dict:
    from katcha.services.acquisition_handoff import finalize_job

    channel, identity = uuid.UUID(channel_id), uuid.UUID(job_id)
    row = jobs.get_job(channel, identity)
    if row.status == "awaiting_validation":
        # Independent streaming hash/probe may take time for large files. Heartbeat
        # in the activity thread's copied context so Temporal can recover a crash.
        import contextvars

        stop = threading.Event()
        context = contextvars.copy_context()

        def pulse():
            while not stop.wait(15):
                context.run(activity.heartbeat, {"job_id": job_id, "phase": "validating"})

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            activity.heartbeat({"job_id": job_id, "phase": "validating"})
            try:
                row = finalize_job(channel, identity)
            except jobs.JobConflict:
                from katcha.services.acquisition_handoff import quarantine_validation

                row = quarantine_validation(channel, identity)
        finally:
            stop.set()
            thread.join(timeout=1)
    elif row.status == "leased" and jobs._utc(row.lease_expires_at) < jobs._now() - timedelta(
        minutes=5
    ):
        row = jobs.reconcile_expired_job(channel, identity, "primary-supervisor")
    return {"job_id": str(row.id), "status": row.status, "failure_code": row.failure_code}


async def supervise_acquisitions(client, settings):
    """Durable DB is the outbox: restart repairs dispatch without starting provider work."""
    import logging

    while True:
        try:

            def pending():
                with session_scope() as session:
                    return list(
                        session.execute(
                            select(AcquisitionJob.id, AcquisitionJob.channel_profile_id)
                            .where(
                                AcquisitionJob.status.in_(
                                    ["queued", "leased", "awaiting_validation"]
                                )
                            )
                            .order_by(
                                case((AcquisitionJob.status == "awaiting_validation", 0), else_=1),
                                AcquisitionJob.created_at,
                            )
                            .limit(1000)
                        )
                    )

            identities = await asyncio.to_thread(pending)
            for job_id, channel in identities:
                with contextlib.suppress(WorkflowAlreadyStartedError):
                    await client.start_workflow(
                        "RemoteAcquisitionWorkflow",
                        args=[str(channel), str(job_id)],
                        id=f"acquisition-{job_id}",
                        task_queue=settings.temporal_task_queue,
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                    )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Acquisition dispatch unavailable type=%s", type(exc).__name__
            )
        await asyncio.sleep(20)
