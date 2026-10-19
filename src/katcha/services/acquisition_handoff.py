"""Lease-fenced, resumable S3/R2 handoff. Only primary workers validate/promote."""

from __future__ import annotations

import base64
import binascii
import math
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from katcha.acquisition.job_contracts import LeaseMessage, PartView, ReportJob, TransferView
from katcha.acquisition_job_models import AcquisitionJob, AcquisitionTransfer
from katcha.acquisition_models import DiscoveryCandidate
from katcha.db import session_scope
from katcha.editorial.media_intake.validation import (
    MAX_DIMENSION,
    MAX_DURATION_SECONDS,
    MAX_IMAGE_PIXELS,
    _image_hash,
)
from katcha.editorial_models import MediaAssetValidation
from katcha.integrations.object_streaming import (
    PART_BYTES,
    SeekableObjectReader,
    verify_stored_sha256,
)
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, SourceItem
from katcha.runtime_fence import assert_mutation_authority
from katcha.services import acquisition_jobs as jobs


def bind_source(session, receipt, job):
    """The operator's enqueue links an existing candidate without changing rights."""
    candidate = session.get(DiscoveryCandidate, receipt.candidate_id)
    source = session.get(SourceItem, candidate.source_item_id) if candidate.source_item_id else None
    if source is None:
        source = session.scalar(
            select(SourceItem).where(SourceItem.source_url == candidate.canonical_url)
        )
    if source is not None:
        metadata = source.source_metadata or {}
        if str(metadata.get("channel_profile_id")) != str(job.channel_profile_id):
            raise jobs.JobConflict("Existing source is not owned by this channel")
        previous_id = metadata.get("remote_acquisition_job_id")
        if previous_id not in {None, str(job.id)}:
            previous = session.get(AcquisitionJob, uuid.UUID(previous_id))
            previous_receipt = (
                jobs._receipt(session, job.channel_profile_id, previous.receipt_id)
                if previous
                else None
            )
            if (
                previous is None
                or previous.channel_profile_id != job.channel_profile_id
                or not (
                    previous.status == "failed"
                    and previous_receipt.status == "failed"
                    or previous.status == "cancelled"
                    and previous.failure_code == "operator_released"
                )
            ):
                raise jobs.JobConflict(
                    "Reconcile this source's previous acquisition before retrying"
                )
        if source.status == "ingesting" and metadata.get("remote_acquisition_job_id") != str(
            job.id
        ):
            raise jobs.JobConflict(
                "Source has active local acquisition; reconcile it before remote dispatch"
            )
        if source.clip_id is not None:
            raise jobs.JobConflict("Source already has media; use its existing acquisition")
    else:
        source = SourceItem(
            id=uuid.uuid4(),
            source_url=candidate.canonical_url,
            canonical_url=candidate.canonical_url,
            platform=candidate.platform,
            title=candidate.title,
            creator=candidate.creator,
            source_metadata={},
        )
        session.add(source)
        session.flush()
    source.source_metadata = {
        **dict(source.source_metadata or {}),
        "channel_profile_id": str(job.channel_profile_id),
        "discovery_candidate_id": str(candidate.id),
        "acquisition_managed": True,
        "acquisition_purpose": "review",
        "remote_acquisition_job_id": str(job.id),
    }
    source.status, source.error = "registered", None
    candidate.source_item_id = source.id
    candidate.status = "promoted"
    job.source_item_id = source.id


def _owned(session, channel, job_id, worker, identity):
    row = session.scalar(
        select(AcquisitionJob)
        .where(AcquisitionJob.id == job_id, AcquisitionJob.channel_profile_id == channel)
        .with_for_update()
    )
    if row is None:
        raise jobs.JobNotFound("Acquisition job not found in this channel")
    if (
        row.status != "leased"
        or row.worker_principal != worker
        or row.claim_id != identity.claim_id
        or row.generation != identity.generation
        or jobs._utc(row.lease_expires_at) <= jobs._now()
    ):
        raise jobs.JobConflict("Upload lease is expired or owned by another execution")
    receipt = jobs._receipt(session, channel, row.receipt_id)
    if receipt.status != "acquiring":
        raise jobs.JobConflict("Receipt is no longer acquiring")
    jobs._permission(session, receipt)
    return row


def _params(store, row, transfer):
    return dict(
        Bucket=store.settings.s3_bucket,
        Key=jobs.staging_key(row, generation=transfer.generation),
        UploadId=transfer.upload_id,
    )


def _parts(store, row, transfer):
    result = {}
    marker = 0
    while True:
        response = store.client.list_parts(**_params(store, row, transfer), PartNumberMarker=marker)
        for part in response.get("Parts", []):
            result[int(part["PartNumber"])] = part
        if not response.get("IsTruncated"):
            return result
        next_marker = int(response["NextPartNumberMarker"])
        if next_marker <= marker or len(result) > 10000:
            raise jobs.JobConflict("Invalid multipart cursor")
        marker = next_marker


def _matches(store, row, transfer):
    """Reconcile completion whose response or database commit may have been lost."""
    from botocore.exceptions import ClientError

    try:
        head = store.client.head_object(
            Bucket=store.settings.s3_bucket,
            Key=jobs.staging_key(row, generation=transfer.generation),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return (
        int(head.get("ContentLength", -1)) == transfer.size_bytes
        and head.get("Metadata", {}).get("acquisition-sha256") == transfer.sha256
        and head.get("Metadata", {}).get("acquisition-generation") == str(transfer.generation)
    )


def prepare_upload(channel, job_id, worker, message: ReportJob, *, store=None):
    store = store or ObjectStore()
    with session_scope() as session:
        row = _owned(session, channel, job_id, worker, message)
        if message.size_bytes > row.max_bytes:
            raise jobs.JobConflict("Media exceeds the acquisition byte limit")
        transfer = session.get(AcquisitionTransfer, row.id)
        if transfer is None:
            assert_mutation_authority(
                "storage.acquisition_multipart_create", settings=store.settings
            )
            response = store.client.create_multipart_upload(
                Bucket=store.settings.s3_bucket,
                Key=jobs.staging_key(row),
                ContentType="application/octet-stream",
                Metadata={
                    "acquisition-sha256": message.sha256,
                    "acquisition-generation": str(row.generation),
                },
            )
            transfer = AcquisitionTransfer(
                job_id=row.id,
                generation=row.generation,
                size_bytes=message.size_bytes,
                sha256=message.sha256,
                upload_id=response["UploadId"],
                parts={},
                completed=False,
            )
            session.add(transfer)
            session.flush()
        if transfer.sha256 != message.sha256 or transfer.size_bytes != message.size_bytes:
            raise jobs.JobConflict("An upload already exists with different media identity")
        row.phase = "uploading"
        if not transfer.completed and _matches(store, row, transfer):
            transfer.completed = True
        parts = {} if transfer.completed else _parts(store, row, transfer)
        uploaded = [
            number
            for number, part in parts.items()
            if str(number) in transfer.parts and part["Size"] == transfer.parts[str(number)]["size"]
        ]
        return TransferView(
            part_bytes=PART_BYTES,
            sha256=transfer.sha256,
            size_bytes=transfer.size_bytes,
            uploaded_parts=sorted(uploaded),
            completed=transfer.completed,
        )


def authorize_part(channel, job_id, worker, identity, number, md5, *, store=None):
    try:
        if len(base64.b64decode(md5, validate=True)) != 16:
            raise ValueError
    except (ValueError, binascii.Error):
        raise jobs.JobConflict("Part checksum must be a base64 MD5 digest") from None
    store = store or ObjectStore()
    with session_scope() as session:
        row = _owned(session, channel, job_id, worker, identity)
        transfer = session.get(AcquisitionTransfer, row.id)
        if transfer is None or transfer.completed:
            raise jobs.JobConflict("Start an unfinished upload before requesting parts")
        count = math.ceil(transfer.size_bytes / PART_BYTES)
        if not 1 <= number <= count:
            raise jobs.JobConflict("Part number is outside this upload")
        size = min(PART_BYTES, transfer.size_bytes - (number - 1) * PART_BYTES)
        expected = {"md5": md5, "size": size}
        previous = transfer.parts.get(str(number))
        if previous is not None and previous != expected:
            raise jobs.JobConflict("This part already has a different checksum")
        # Freeze the checksum in the transaction that releases the signed URL.
        transfer.parts = {**transfer.parts, str(number): expected}
        assert_mutation_authority("storage.acquisition_part_grant", settings=store.settings)
        url = store.client.generate_presigned_url(
            "upload_part",
            Params={
                **_params(store, row, transfer),
                "PartNumber": number,
                "ContentLength": size,
                "ContentMD5": md5,
            },
            ExpiresIn=max(
                1, min(60, int((jobs._utc(row.lease_expires_at) - jobs._now()).total_seconds()))
            ),
        )
        return PartView(url=url, headers={"Content-Length": str(size), "Content-MD5": md5})


def complete_upload(channel, job_id, worker, identity: LeaseMessage, *, store=None):
    store = store or ObjectStore()
    with session_scope() as session:
        row = jobs._job(session, channel, job_id)
        if (
            row.status in {"awaiting_validation", "completed"}
            and row.worker_principal == worker
            and row.claim_id == identity.claim_id
            and row.generation == identity.generation
        ):
            return row
        row = _owned(session, channel, job_id, worker, identity)
        transfer = session.get(AcquisitionTransfer, row.id)
        if transfer is None:
            raise jobs.JobConflict("Upload not found")
        if not transfer.completed:
            if not _matches(store, row, transfer):
                parts = _parts(store, row, transfer)
                count = math.ceil(transfer.size_bytes / PART_BYTES)
                if set(parts) != set(range(1, count + 1)) or any(
                    str(n) not in transfer.parts or part["Size"] != transfer.parts[str(n)]["size"]
                    for n, part in parts.items()
                ):
                    raise jobs.JobConflict("Upload is incomplete; resume the missing parts")
                assert_mutation_authority("storage.acquisition_complete", settings=store.settings)
                store.client.complete_multipart_upload(
                    **_params(store, row, transfer),
                    MultipartUpload={
                        "Parts": [
                            {"PartNumber": n, "ETag": parts[n]["ETag"]} for n in sorted(parts)
                        ]
                    },
                )
                if not _matches(store, row, transfer):
                    raise jobs.JobConflict("Completed object identity does not match its upload")
            transfer.completed = True
        sha, size = transfer.sha256, transfer.size_bytes
    return jobs.report_job(
        channel, job_id, worker, ReportJob(**identity.model_dump(), sha256=sha, size_bytes=size)
    )


def probe_object(store, key, size, media_type):
    """Probe actual bytes through bounded range reads, including a decoded AV frame."""
    import av
    from PIL import Image

    if media_type == "image":
        with SeekableObjectReader(store, key, size) as reader, Image.open(reader) as image:
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP"}:
                raise ValueError("Image format is not supported")
            width, height = image.size
            if not (
                0 < width <= MAX_DIMENSION
                and 0 < height <= MAX_DIMENSION
                and width * height <= MAX_IMAGE_PIXELS
            ):
                raise ValueError("Image dimensions exceed validation limits")
            image.verify()
        with SeekableObjectReader(store, key, size) as reader, Image.open(reader) as image:
            phash = _image_hash(image)
            mime = Image.MIME.get(image.format, "application/octet-stream")
        return {"width": width, "height": height}, mime, None, width, height, phash
    with (
        SeekableObjectReader(store, key, size) as reader,
        av.open(
            reader,
            mode="r",
            options={
                "protocol_whitelist": "file",
                "format_whitelist": "mov,matroska,webm,avi,mpegts,ogg,mp3,wav,flac,aac,flv",
            },
        ) as container,
    ):
        stream = next((s for s in container.streams if s.type == media_type), None)
        if stream is None:
            raise ValueError("Media does not contain its expected stream")
        duration = float(container.duration / av.time_base) if container.duration else 0
        if not duration and stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        if not 0 < duration <= MAX_DURATION_SECONDS:
            raise ValueError("Media duration is outside supported limits")
        width = int(stream.width) if media_type == "video" else None
        height = int(stream.height) if media_type == "video" else None
        if media_type == "video" and not (
            0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION
        ):
            raise ValueError("Media dimensions are outside supported limits")
        stream.codec_context.thread_count = 1
        if next(container.decode(stream), None) is None:
            raise ValueError("Media contains no decodable frames")
        name = str(container.format.name or "")
        mime = (
            (
                "video/mp4"
                if "mp4" in name or "mov" in name
                else "video/webm"
                if "webm" in name
                else "video/x-matroska"
            )
            if media_type == "video"
            else ("audio/mpeg" if "mp3" in name else "audio/wav" if "wav" in name else "audio/ogg")
        )
        probe = {
            "format": {"duration": duration, "format_name": name},
            "streams": [{"codec_type": media_type, "width": width, "height": height}],
        }
        return probe, mime, duration, width, height, None


def finalize_job(channel, job_id, *, store=None):
    """Replay-safe primary-side promotion; no provider request is ever made here."""
    store = store or ObjectStore()
    with session_scope() as session:
        row = jobs._job(session, channel, job_id)
        if row.status == "completed":
            return row
        if row.status != "awaiting_validation":
            raise jobs.JobConflict("Media has not been handed off for validation")
        receipt = jobs._receipt(session, channel, row.receipt_id)
        jobs._permission(session, receipt)
        if row.source_item_id is None:
            bind_source(session, receipt, row)
        transfer = session.get(AcquisitionTransfer, row.id)
        if (
            transfer is None
            or not transfer.completed
            or row.pending_result.get("sha256") != transfer.sha256
            or row.pending_result.get("size_bytes") != transfer.size_bytes
        ):
            raise jobs.JobConflict("No matching completed media transfer exists")
        generation, sha, size, media_type = (
            row.generation,
            transfer.sha256,
            transfer.size_bytes,
            row.media_type,
        )
        key = jobs.staging_key(row, generation=transfer.generation)
        if row.pending_result.get("object_store_key") != key:
            raise jobs.JobConflict("Reported staging identity does not match the transfer")
    # A staging multipart upload is immutable after completion. Worker grants cannot rewrite it.
    import av
    from PIL import Image, UnidentifiedImageError

    try:
        verify_stored_sha256(store, key, sha, size)
        probe, mime, duration, width, height, phash = probe_object(store, key, size, media_type)
    except (
        ValueError,
        av.FFmpegError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        EOFError,
    ):
        reject_job(channel, job_id, generation, "invalid_media")
        return jobs.get_job(channel, job_id)
    canonical = f"media/sha256/{sha}"
    # Copy through the provider's managed server-side multipart API. Keep accepted
    # objects out of the staging namespace, and verify persisted canonical bytes.
    store.copy(key, canonical, content_type=mime)
    verify_stored_sha256(store, canonical, sha, size)
    with session_scope() as session:
        row = session.scalar(
            select(AcquisitionJob)
            .where(AcquisitionJob.id == job_id, AcquisitionJob.channel_profile_id == channel)
            .with_for_update()
        )
        if row.status == "completed":
            return row
        if row.status != "awaiting_validation" or row.generation != generation:
            raise jobs.JobConflict("Acquisition changed during media validation")
        receipt = jobs._receipt(session, channel, row.receipt_id)
        jobs._permission(session, receipt)
        if receipt.status != "acquiring":
            raise jobs.JobConflict("Receipt changed during media validation")
        source = session.get(SourceItem, row.source_item_id)
        if source is None or str((source.source_metadata or {}).get("channel_profile_id")) != str(
            channel
        ):
            raise jobs.JobConflict("Acquisition source is not owned by this channel")
        clip = session.scalar(select(Clip).where(Clip.sha256 == sha))
        if clip is None:
            clip = Clip(
                id=uuid.uuid4(),
                sha256=sha,
                storage_key=canonical,
                extension={
                    "video/mp4": "mp4",
                    "image/png": "png",
                    "image/jpeg": "jpg",
                    "audio/mpeg": "mp3",
                    "video/webm": "webm",
                }.get(mime),
                size_bytes=size,
                duration_seconds=Decimal(str(duration)) if duration else None,
                width=width,
                height=height,
                media_metadata=probe,
            )
            try:
                with session.begin_nested():
                    session.add(clip)
                    session.flush()
            except IntegrityError:
                clip = session.scalar(select(Clip).where(Clip.sha256 == sha))
                if clip is None:
                    raise
        # Global byte deduplication does not transfer rights or channel ownership.
        # The canonical object was independently verified even when the Clip exists.
        clip.storage_key = canonical
        source.clip_id, source.status, source.error = clip.id, "ready", None
        source.source_metadata = {
            **dict(source.source_metadata or {}),
            "acquisition_receipt_id": str(receipt.id),
        }
        receipt.status, receipt.sha256, receipt.bytes_received = "succeeded", sha, size
        receipt.mime, receipt.object_store_key, receipt.media_probe = mime, canonical, probe
        receipt.response_time, receipt.error_category = jobs._now(), None
        duplicate = session.scalar(
            select(MediaAssetValidation)
            .where(
                MediaAssetValidation.channel_profile_id == channel,
                MediaAssetValidation.actual_sha256 == sha,
                MediaAssetValidation.status == "validated",
            )
            .order_by(MediaAssetValidation.created_at)
        )
        session.add(
            MediaAssetValidation(
                id=uuid.uuid4(),
                receipt_id=receipt.id,
                channel_profile_id=channel,
                probe_version=1,
                status="validated",
                media_type=media_type,
                expected_sha256=sha,
                actual_sha256=sha,
                perceptual_hash=phash,
                duplicate_of_id=duplicate.id if duplicate else None,
                mime=mime,
                size_bytes=size,
                duration_seconds=duration,
                width=width,
                height=height,
                probe=probe,
            )
        )
        row.status, row.phase, row.failure_code = "completed", "completed", None
        jobs._event(session, row, "completed", "primary-validator")
    # Commit the accepted receipt and asset before deleting temporary media.
    import logging

    try:
        store.delete(key)
    except Exception:
        logging.getLogger(__name__).warning(
            "Accepted acquisition staging cleanup pending job=%s", job_id
        )
    return row


def reject_job(channel, job_id, generation, code):
    with session_scope() as session:
        row = jobs._change(
            session,
            channel,
            job_id,
            AcquisitionJob.status == "awaiting_validation",
            AcquisitionJob.generation == generation,
            status="failed",
            phase="failed",
            failure_code=code,
        )
        jobs._close_receipt(session, row, status="failed", code=code)
        jobs._event(session, row, "validation_rejected", "primary-validator")


def cleanup_transfer(channel, job_id, *, store=None):
    """Operator cleanup of unaccepted bytes. Never remove completed canonical media."""
    store = store or ObjectStore()
    with session_scope() as session:
        row = jobs._job(session, channel, job_id)
        if row.status not in {"failed", "cancelled", "recovery_required"}:
            raise jobs.JobConflict("Only stopped, unaccepted transfers can be cleaned up")
        transfer = session.get(AcquisitionTransfer, job_id)
        if transfer is None:
            return row
        # Cancellation increments generation; use the transfer's original immutable prefix.
        key = f"acquisition-staging/{channel}/{job_id}/{transfer.generation}/media"
        assert_mutation_authority("storage.acquisition_cleanup", settings=store.settings)
        from botocore.exceptions import ClientError

        try:
            store.client.abort_multipart_upload(
                Bucket=store.settings.s3_bucket, Key=key, UploadId=transfer.upload_id
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
                raise
        store.delete(key)
        session.delete(transfer)
        return row


def recover_upload(channel, job_id, worker, identity: ReportJob):
    """Resume frozen bytes after restart, including a quarantined lost worker.

    Quarantined provider execution cannot be replayed. Only the original owner
    with a matching durable upload and exact local file identity can reopen it.
    A new generation fences every prior process before upload continuation.
    """
    from datetime import timedelta

    with session_scope() as session:
        row = session.scalar(
            select(AcquisitionJob)
            .where(AcquisitionJob.id == job_id, AcquisitionJob.channel_profile_id == channel)
            .with_for_update()
        )
        if row is None:
            raise jobs.JobNotFound("Acquisition job not found in this channel")
        transfer = session.get(AcquisitionTransfer, row.id)
        quarantined = row.status == "recovery_required" and row.failure_code == "worker_lost"
        matching = (
            row.worker_principal == worker
            and row.claim_id == identity.claim_id
            and identity.size_bytes <= row.max_bytes
            and (
                transfer is None
                or (
                    transfer.sha256 == identity.sha256
                    and transfer.size_bytes == identity.size_bytes
                )
            )
        )
        if (
            not matching
            or row.status not in {"leased", "recovery_required"}
            or (quarantined and (transfer is None or identity.generation >= row.generation))
            or (
                not quarantined
                and (row.status != "leased" or row.generation != identity.generation)
            )
        ):
            raise jobs.JobConflict("Only this execution's known downloaded media can resume upload")
        receipt = jobs._receipt(session, channel, row.receipt_id)
        permission = jobs._permission(session, receipt)
        if quarantined:
            if receipt.status != "unknown" or receipt.error_category != "worker_lost":
                raise jobs.JobConflict("Existing provider outcome requires operator reconciliation")
            receipt.status, receipt.error_category, receipt.response_time = "acquiring", None, None
            row.status, row.failure_code = "leased", None
            row.generation += 1
        elif receipt.status != "acquiring":
            raise jobs.JobConflict("Receipt changed; reconcile existing work")
        elif jobs._utc(row.lease_expires_at) > jobs._now():
            return row, receipt.fetch_url_if_allowed, permission
        row.phase = "uploading"
        row.lease_expires_at = jobs._now() + timedelta(seconds=jobs.LEASE_SECONDS)
        jobs._event(session, row, "upload_recovered", worker)
        return row, receipt.fetch_url_if_allowed, permission


def staging_key_for_job(row):
    with session_scope() as session:
        transfer = session.get(AcquisitionTransfer, row.id)
        return jobs.staging_key(row, generation=transfer.generation if transfer else None)


def release_source(channel, job_id, actor):
    """Explicit operator acknowledgement allows a *new* authorized attempt.

    The previous receipt remains failed/unknown. No provider is called or replayed,
    no success is invented, and all outstanding leases/grants remain fenced.
    """
    with session_scope() as session:
        row = jobs._job(session, channel, job_id)
        if row.status == "cancelled" and row.failure_code == "operator_released":
            return row
        if row.status not in {"failed", "cancelled", "recovery_required"}:
            raise jobs.JobConflict(
                "Stop and reconcile active acquisition before releasing its source"
            )
        row = jobs._change(
            session,
            channel,
            job_id,
            AcquisitionJob.status == row.status,
            AcquisitionJob.generation == row.generation,
            status="cancelled",
            phase="cancelled",
            failure_code="operator_released",
            generation=row.generation + 1,
            lease_expires_at=None,
        )
        jobs._event(session, row, "source_released_after_operator_review", actor)
        return row


def quarantine_validation(channel, job_id):
    """A durable actionable stop for invalid linkage/revoked permission, never a replay."""
    with session_scope() as session:
        row = jobs._job(session, channel, job_id)
        if row.status != "awaiting_validation":
            return row
        row = jobs._change(
            session,
            channel,
            job_id,
            AcquisitionJob.status == "awaiting_validation",
            AcquisitionJob.generation == row.generation,
            status="recovery_required",
            phase="recovery_required",
            failure_code="handoff_reconciliation_required",
            generation=row.generation + 1,
        )
        jobs._close_receipt(session, row, status="unknown", code="handoff_reconciliation_required")
        jobs._event(session, row, "validation_needs_reconciliation", "primary-validator")
        return row
