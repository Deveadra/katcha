"""Durable remote execution ownership, with no implicit retry of uncertain work.

This control plane reserves canonical AcquisitionReceipt records. It does not
download media, mint storage access, solve challenges, or validate worker claims.
Every mutation emits a credential-free DomainEvent in the same transaction.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from katcha.acquisition.job_contracts import (
    ClaimJob,
    EnqueueJob,
    FailJob,
    HeartbeatJob,
    LeaseMessage,
    ReportJob,
)
from katcha.acquisition_job_models import AcquisitionJob
from katcha.acquisition_models import DiscoveryCandidate
from katcha.db import session_scope
from katcha.editorial.media_intake.contracts import ProviderPermissionV1
from katcha.editorial_models import AcquisitionReceipt
from katcha.models import DomainEvent

LEASE_SECONDS = 90


class JobNotFound(ValueError):
    pass


class JobConflict(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _job(session: Session, channel_id: uuid.UUID, job_id: uuid.UUID) -> AcquisitionJob:
    row = session.scalar(
        select(AcquisitionJob).where(
            AcquisitionJob.id == job_id,
            AcquisitionJob.channel_profile_id == channel_id,
        )
    )
    if row is None:
        raise JobNotFound("Acquisition job not found in this channel")
    return row


def _receipt(session: Session, channel_id: uuid.UUID, receipt_id: uuid.UUID) -> AcquisitionReceipt:
    row = session.scalar(
        select(AcquisitionReceipt).where(
            AcquisitionReceipt.id == receipt_id,
            AcquisitionReceipt.channel_profile_id == channel_id,
        )
    )
    if row is None:
        raise JobNotFound("Acquisition receipt not found in this channel")
    return row


def _permission(session: Session, receipt: AcquisitionReceipt) -> ProviderPermissionV1:
    scope = receipt.authorization_scope or {}
    try:
        permission = ProviderPermissionV1.model_validate(
            {key: scope[key] for key in ProviderPermissionV1.model_fields if key in scope}
        )
        url = urlsplit(receipt.fetch_url_if_allowed or "")
        host = url.hostname or ""
        candidate = session.get(DiscoveryCandidate, receipt.candidate_id)
        allowed = (
            permission.allows("download")
            and permission.allows_host(host)
            and url.scheme == "https"
            and url.port in {None, 443}
            and not url.username
            and not url.password
            and not url.fragment
            and candidate is not None
            and candidate.adapter_key == permission.provider
            and str((candidate.candidate_metadata or {}).get("channel_profile_id"))
            == str(receipt.channel_profile_id)
        )
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            allowed = allowed and "." in host and not host.endswith(".localhost")
        else:
            allowed = allowed and address.is_global and not address.is_multicast
    except (ValueError, TypeError):
        raise JobConflict("Download permission or approved HTTPS origin is invalid") from None
    if not allowed:
        raise JobConflict("Download permission or approved HTTPS origin is invalid")
    # DNS and redirect checks must still run on the acquisition host at connection
    # time through PublicDestinationTransport; enqueue is not an SSRF attestation.
    return permission


def _event(session: Session, row: AcquisitionJob, action: str, actor: str) -> None:
    session.add(
        DomainEvent(
            aggregate_type="acquisition_job",
            aggregate_id=str(row.id),
            event_type=f"acquisition_job.{action}",
            payload={
                "schema_version": 1,
                "job_id": str(row.id),
                "receipt_id": str(row.receipt_id),
                "channel_profile_id": str(row.channel_profile_id),
                "status": row.status,
                "generation": row.generation,
                "failure_code": row.failure_code,
                "actor": actor,
            },
        )
    )


def _change(session: Session, channel_id: uuid.UUID, job_id: uuid.UUID, *conditions, **values):
    changed = session.scalar(
        update(AcquisitionJob)
        .where(
            AcquisitionJob.id == job_id,
            AcquisitionJob.channel_profile_id == channel_id,
            *conditions,
        )
        .values(**values)
        .returning(AcquisitionJob.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise JobConflict("Job changed, lease expired, or execution owner does not match")
    session.expire_all()
    return _job(session, channel_id, job_id)


def _leased(worker: str, message: LeaseMessage, now: datetime) -> tuple:
    return (
        AcquisitionJob.status == "leased",
        AcquisitionJob.worker_principal == worker,
        AcquisitionJob.generation == message.generation,
        AcquisitionJob.claim_id == message.claim_id,
        AcquisitionJob.lease_expires_at > now,
        AcquisitionJob.receipt_id.in_(
            select(AcquisitionReceipt.id).where(
                AcquisitionReceipt.status == "acquiring",
            )
        ),
    )


def staging_key(row: AcquisitionJob) -> str:
    return f"acquisition-staging/{row.channel_profile_id}/{row.id}/{row.generation}/media"


def enqueue_job(
    channel_id: uuid.UUID,
    message: EnqueueJob,
    actor: str = "system",
) -> AcquisitionJob:
    with session_scope() as session:
        receipt = _receipt(session, channel_id, message.receipt_id)
        existing = session.scalar(
            select(AcquisitionJob).where(
                AcquisitionJob.receipt_id == receipt.id,
            )
        )
        if existing is not None:
            if existing.max_bytes != message.max_bytes:
                raise JobConflict("This receipt already has a different acquisition byte limit")
            return existing
        _permission(session, receipt)
        reserved = session.scalar(
            update(AcquisitionReceipt)
            .where(
                AcquisitionReceipt.id == receipt.id,
                AcquisitionReceipt.status == "authorized",
            )
            .values(status="acquiring")
            .returning(AcquisitionReceipt.id)
            .execution_options(synchronize_session=False)
        )
        if reserved is None:
            # A concurrent enqueue may have reserved this receipt while we read.
            session.expire_all()
            existing = session.scalar(
                select(AcquisitionJob).where(
                    AcquisitionJob.receipt_id == receipt.id,
                )
            )
            if existing is not None and existing.max_bytes == message.max_bytes:
                return existing
            raise JobConflict("Receipt is active or final; reconcile it before starting work")
        row = AcquisitionJob(
            receipt_id=receipt.id,
            channel_profile_id=channel_id,
            max_bytes=message.max_bytes,
        )
        session.add(row)
        session.flush()
        _event(session, row, "queued", actor)
        return row


def get_job(channel_id: uuid.UUID, job_id: uuid.UUID) -> AcquisitionJob:
    with session_scope() as session:
        return _job(session, channel_id, job_id)


def list_jobs(
    channel_id: uuid.UUID,
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[AcquisitionJob]:
    with session_scope() as session:
        query = select(AcquisitionJob).where(
            AcquisitionJob.channel_profile_id == channel_id,
        )
        if status is not None:
            query = query.where(AcquisitionJob.status == status)
        return list(
            session.scalars(
                query.order_by(
                    AcquisitionJob.created_at.desc(),
                    AcquisitionJob.id,
                ).limit(limit)
            )
        )


def claim_job(
    channel_id: uuid.UUID,
    job_id: uuid.UUID,
    worker: str,
    message: ClaimJob,
) -> tuple[AcquisitionJob, str, ProviderPermissionV1]:
    now = _now()
    with session_scope() as session:
        row = _job(session, channel_id, job_id)
        receipt = _receipt(session, channel_id, row.receipt_id)
        permission = _permission(session, receipt)
        if receipt.status != "acquiring":
            raise JobConflict("Receipt is no longer available for remote execution")
        if row.status == "leased" and row.claim_id == message.claim_id:
            if row.worker_principal != worker or _utc(row.lease_expires_at) <= now:
                raise JobConflict("The original lease is expired or owned by another worker")
            return row, receipt.fetch_url_if_allowed, permission
        row = _change(
            session,
            channel_id,
            job_id,
            AcquisitionJob.status == "queued",
            AcquisitionJob.generation == row.generation,
            AcquisitionJob.receipt_id.in_(
                select(AcquisitionReceipt.id).where(
                    AcquisitionReceipt.status == "acquiring",
                )
            ),
            status="leased",
            generation=row.generation + 1,
            worker_principal=worker,
            claim_id=message.claim_id,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            last_heartbeat_at=now,
            phase="acquiring",
        )
        _event(session, row, "claimed", worker)
        return row, receipt.fetch_url_if_allowed, permission


def heartbeat_job(
    channel_id: uuid.UUID,
    job_id: uuid.UUID,
    worker: str,
    message: HeartbeatJob,
) -> AcquisitionJob:
    now = _now()
    with session_scope() as session:
        row = _job(session, channel_id, job_id)
        receipt = _receipt(session, channel_id, row.receipt_id)
        _permission(session, receipt)
        if receipt.status != "acquiring":
            raise JobConflict("Receipt is no longer available for remote execution")
        # Identical retries never extend a lease or move progress backwards.
        if message.sequence == row.heartbeat_sequence:
            if (
                row.status == "leased"
                and row.worker_principal == worker
                and row.claim_id == message.claim_id
                and row.generation == message.generation
                and _utc(row.lease_expires_at) > now
                and row.bytes_received == message.bytes_received
                and row.phase == message.phase
            ):
                return row
            raise JobConflict("Heartbeat identity or progress does not match")
        row = _change(
            session,
            channel_id,
            job_id,
            *_leased(worker, message, now),
            AcquisitionJob.heartbeat_sequence < message.sequence,
            AcquisitionJob.bytes_received <= message.bytes_received,
            AcquisitionJob.max_bytes >= message.bytes_received,
            heartbeat_sequence=message.sequence,
            bytes_received=message.bytes_received,
            phase=message.phase,
            last_heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
        )
        # Heartbeats update durable progress without creating an unbounded event stream.
        return row


def report_job(
    channel_id: uuid.UUID,
    job_id: uuid.UUID,
    worker: str,
    message: ReportJob,
) -> AcquisitionJob:
    now = _now()
    with session_scope() as session:
        row = _job(session, channel_id, job_id)
        result = {
            "sha256": message.sha256,
            "size_bytes": message.size_bytes,
            "object_store_key": staging_key(row),
        }
        receipt = _receipt(session, channel_id, row.receipt_id)
        _permission(session, receipt)
        if receipt.status != "acquiring":
            raise JobConflict("Receipt is no longer available for remote execution")
        if (
            row.status == "awaiting_validation"
            and row.worker_principal == worker
            and row.claim_id == message.claim_id
            and row.generation == message.generation
            and row.pending_result == result
        ):
            return row
        row = _change(
            session,
            channel_id,
            job_id,
            *_leased(worker, message, now),
            AcquisitionJob.max_bytes >= message.size_bytes,
            AcquisitionJob.bytes_received <= message.size_bytes,
            status="awaiting_validation",
            phase="awaiting_validation",
            pending_result=result,
            bytes_received=message.size_bytes,
            lease_expires_at=None,
        )
        _event(session, row, "result_reported", worker)
        # The receipt stays acquiring. Independent object/probe validation must
        # happen before a canonical asset, successful receipt, or resume signal.
        return row


def _close_receipt(session: Session, row: AcquisitionJob, *, status: str, code: str) -> None:
    changed = session.scalar(
        update(AcquisitionReceipt)
        .where(
            AcquisitionReceipt.id == row.receipt_id,
            AcquisitionReceipt.status == "acquiring",
        )
        .values(status=status, error_category=code, response_time=_now())
        .returning(AcquisitionReceipt.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise JobConflict("Receipt changed; reconcile its existing outcome before proceeding")


def fail_job(
    channel_id: uuid.UUID,
    job_id: uuid.UUID,
    worker: str,
    message: FailJob,
) -> AcquisitionJob:
    uncertain = message.code in {"transport_error", "session_lost", "outcome_unknown"}
    status = "recovery_required" if uncertain else "failed"
    with session_scope() as session:
        row = _job(session, channel_id, job_id)
        if (
            row.status == status
            and row.failure_code == message.code
            and row.worker_principal == worker
            and row.claim_id == message.claim_id
            and row.generation == message.generation
        ):
            return row
        row = _change(
            session,
            channel_id,
            job_id,
            *_leased(worker, message, _now()),
            status=status,
            phase=status,
            failure_code=message.code,
            lease_expires_at=None,
        )
        _close_receipt(session, row, status="unknown" if uncertain else "failed", code=message.code)
        _event(session, row, "failed", worker)
        return row


def cancel_job(
    channel_id: uuid.UUID,
    job_id: uuid.UUID,
    actor: str = "system",
) -> AcquisitionJob:
    with session_scope() as session:
        row = _job(session, channel_id, job_id)
        if row.status == "cancelled":
            return row
        uncertain = row.status != "queued"
        row = _change(
            session,
            channel_id,
            job_id,
            AcquisitionJob.status == row.status,
            AcquisitionJob.status.in_(["queued", "leased", "awaiting_validation"]),
            AcquisitionJob.generation == row.generation,
            status="cancelled",
            phase="cancelled",
            failure_code="cancelled",
            generation=row.generation + 1,
            lease_expires_at=None,
        )
        _close_receipt(session, row, status="unknown" if uncertain else "failed", code="cancelled")
        _event(session, row, "cancelled", actor)
        return row


def reconcile_expired_job(
    channel_id: uuid.UUID,
    job_id: uuid.UUID,
    actor: str = "system",
) -> AcquisitionJob:
    """Quarantine lost execution. Never reclaim a lease by replaying the provider."""
    with session_scope() as session:
        row = _job(session, channel_id, job_id)
        if row.status == "recovery_required":
            return row
        row = _change(
            session,
            channel_id,
            job_id,
            AcquisitionJob.status == "leased",
            AcquisitionJob.lease_expires_at <= _now(),
            status="recovery_required",
            phase="recovery_required",
            failure_code="worker_lost",
            generation=row.generation + 1,
            lease_expires_at=None,
        )
        _close_receipt(session, row, status="unknown", code="worker_lost")
        _event(session, row, "lease_expired", actor)
        return row
