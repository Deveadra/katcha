"""Manual metadata and release plans, independent of AI availability."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from katcha.db import session_scope
from katcha.intelligence_models import ChannelProfile
from katcha.models import DomainEvent
from katcha.publishing_models import Publication
from katcha.services.publications import _normalize_publication_metadata


def channel_time_to_utc(value: str, timezone: str) -> datetime:
    local = datetime.fromisoformat(value)
    if local.tzinfo is not None:
        raise ValueError(
            "Channel-local time must not include an offset; use publish_at for absolute time"
        )
    zone = ZoneInfo(timezone)
    candidates = {
        local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        for fold in (0, 1)
        if local.replace(tzinfo=zone, fold=fold)
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == local
    }
    if len(candidates) != 1:
        raise ValueError(
            "This time is missing or ambiguous during daylight saving. "
            "Choose another time or supply an explicit offset"
        )
    return candidates.pop()


def publication_version(row: Publication) -> int:
    return int((row.raw_status or {}).get("manual_plan_version") or 0)


def save_publication_draft(
    publication_id: uuid.UUID,
    *,
    title: str,
    description: str,
    tags: list[str],
    publish_mode: str,
    publish_at: datetime | None,
    channel_local_time: str | None,
    notify_subscribers: bool,
    made_for_kids: bool,
    contains_synthetic_media: bool,
    late_policy: str,
    expected_version: int,
    actor: str,
) -> Publication:
    with session_scope() as session:
        row = session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if row is None:
            raise ValueError("publication not found")
        if row.stage != "metadata_hold" or row.status != "queued" or row.youtube_video_id:
            raise ValueError(
                "Metadata is locked after upload starts; use the release controls for scheduling"
            )
        if publication_version(row) != expected_version:
            raise ValueError(
                "Another tab changed this plan. Reload the saved plan before saving your edits"
            )
        profile = session.scalar(
            select(ChannelProfile).where(
                ChannelProfile.youtube_connection_id == row.youtube_connection_id
            )
        )
        if channel_local_time:
            if profile is None:
                raise ValueError("Choose a channel before saving a local publication time")
            publish_at = channel_time_to_utc(channel_local_time, profile.timezone)
        if publish_mode not in {"private", "unlisted", "asap", "scheduled"}:
            raise ValueError("Unknown final visibility")
        if publish_mode == "scheduled" and publish_at is None:
            raise ValueError("Choose a scheduled time")
        if publish_mode != "scheduled":
            publish_at = None
        privacy = "public" if publish_mode in {"asap", "scheduled"} else publish_mode
        title, description, tags, privacy, publish_at = _normalize_publication_metadata(
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy,
            publish_at=publish_at,
        )
        row.title, row.description, row.tags = title, description, tags
        row.privacy_status, row.publish_at = privacy, publish_at
        row.notify_subscribers, row.made_for_kids = notify_subscribers, made_for_kids
        row.contains_synthetic_media = contains_synthetic_media
        row.raw_status = {
            **dict(row.raw_status or {}),
            "manual_plan_version": expected_version + 1,
            "late_policy": late_policy,
            "metadata_reviewed": True,
        }
        # Generated metadata lineage is retained as prior input, not attributed to manual edits.
        treatment = dict(row.treatment_metadata or {})
        prior = treatment.pop("preupload_packaging", None) or (
            treatment.get("manual_packaging") or {}
        ).get("prior_variant")
        row.treatment_metadata = {
            **treatment,
            "manual_packaging": {
                "version": expected_version + 1,
                "edited_by": actor,
                "prior_variant": prior,
            },
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(row.id),
                event_type="publication.manual_plan_saved",
                payload={
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "privacy_status": privacy,
                    "publish_at": publish_at.isoformat() if publish_at else None,
                    "notify_subscribers": notify_subscribers,
                    "made_for_kids": made_for_kids,
                    "contains_synthetic_media": contains_synthetic_media,
                    "late_policy": late_policy,
                    "version": expected_version + 1,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def change_live_release(
    publication_id: uuid.UUID,
    *,
    publish_mode: str,
    publish_at: datetime | None,
    channel_local_time: str | None,
    expected_version: int,
    actor: str,
) -> Publication:
    from katcha.integrations.youtube.client import YouTubeClient

    failure: str | None = None
    with session_scope() as session:
        row = session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if row is None:
            raise ValueError("publication not found")
        if not row.youtube_video_id or row.status not in {
            "scheduled",
            "private",
            "unlisted",
            "published",
        }:
            raise ValueError("Wait for YouTube processing before changing the live release plan")
        if publication_version(row) != expected_version:
            raise ValueError("Another tab changed this plan. Reload before saving")
        profile = session.scalar(
            select(ChannelProfile).where(
                ChannelProfile.youtube_connection_id == row.youtube_connection_id
            )
        )
        if channel_local_time:
            if profile is None:
                raise ValueError("Channel timezone is unavailable")
            publish_at = channel_time_to_utc(channel_local_time, profile.timezone)
        if publish_mode not in {"private", "unlisted", "asap", "scheduled"}:
            raise ValueError("Unknown final visibility")
        if publish_at is not None and publish_at.tzinfo is None:
            raise ValueError("publish_at must include a timezone")
        if publish_mode == "scheduled" and (publish_at is None or publish_at <= datetime.now(UTC)):
            raise ValueError("Choose a future time for scheduling")
        if (row.raw_status or {}).get("release_confirmation", {}).get("state") == "uncertain":
            raise ValueError(
                "Reconcile the last release update with YouTube before changing it again"
            )
        client = YouTubeClient(row.youtube_connection_id)
        current = client.video_resource(row.youtube_video_id)
        current_status = dict(current.get("status") or {})
        # A live/public video cannot be newly scheduled through YouTube's publishAt API.
        if publish_mode == "scheduled" and (
            current_status.get("privacyStatus") != "private" or row.published_at
        ):
            raise ValueError(
                "Only private, never-published videos can be scheduled; "
                "open YouTube Studio for this video"
            )
        desired = (
            "public"
            if publish_mode == "asap"
            else ("private" if publish_mode == "scheduled" else publish_mode)
        )
        try:
            if publish_mode == "scheduled":
                client.schedule_video(
                    row.youtube_video_id,
                    publish_at=publish_at,
                    made_for_kids=row.made_for_kids,
                    contains_synthetic_media=row.contains_synthetic_media,
                )
            else:
                client.set_privacy(
                    row.youtube_video_id,
                    privacy_status=desired,
                    made_for_kids=row.made_for_kids,
                    contains_synthetic_media=row.contains_synthetic_media,
                )
            observed = dict(client.video_resource(row.youtube_video_id).get("status") or {})
            confirmed_time = observed.get("publishAt")
            confirmed_at = (
                datetime.fromisoformat(str(confirmed_time).replace("Z", "+00:00"))
                if confirmed_time
                else None
            )
            if (
                observed.get("privacyStatus") != desired
                or (publish_mode == "scheduled" and confirmed_at != publish_at)
                or (publish_mode != "scheduled" and confirmed_at is not None)
            ):
                raise ValueError("YouTube has not confirmed the requested visibility/time")
        except Exception as exc:
            failure = str(exc)[:1000]
            row.raw_status = {
                **dict(row.raw_status or {}),
                "release_confirmation": {
                    "state": "uncertain",
                    "requested_mode": publish_mode,
                    "requested_publish_at": publish_at.isoformat() if publish_at else None,
                    "error": failure,
                },
            }
            row.raw_status = {**row.raw_status, "manual_plan_version": expected_version + 1}
            row.error = "Release change requires reconciliation: " + failure
        else:
            row.privacy_status = "public" if publish_mode == "scheduled" else desired
            row.publish_at = publish_at if publish_mode == "scheduled" else None
            row.status = (
                "scheduled"
                if publish_mode == "scheduled"
                else ("published" if desired == "public" else desired)
            )
            row.stage = row.status
            if desired == "public":
                row.published_at = row.published_at or datetime.now(UTC)
            row.error = None
            row.raw_status = {
                **dict(row.raw_status or {}),
                "manual_plan_version": expected_version + 1,
                "release_confirmation": {"state": "confirmed", "observed": observed},
            }
            session.add(
                DomainEvent(
                    aggregate_type="publication",
                    aggregate_id=str(row.id),
                    event_type="publication.release_plan_confirmed",
                    payload={"actor": actor, "mode": publish_mode, "observed": observed},
                )
            )
        session.flush()
        session.refresh(row)
        session.expunge(row)
    if failure:
        raise ValueError(
            "Release update may have reached YouTube. Refresh/reconcile before retrying: " + failure
        )
    return row


def save_thumbnail(
    publication_id: uuid.UUID, data: bytes, *, expected_version: int, actor: str
) -> Publication:
    import hashlib
    from io import BytesIO

    from PIL import Image, UnidentifiedImageError

    from katcha.integrations.storage import ObjectStore

    if not data or len(data) > 2 * 1024 * 1024:
        raise ValueError("Choose a JPEG or PNG thumbnail up to 2 MiB")
    try:
        with Image.open(BytesIO(data)) as image:
            kind = image.format
            if kind not in {"JPEG", "PNG"} or image.width * image.height > 40_000_000:
                raise ValueError("Choose a JPEG or PNG thumbnail within 40 megapixels")
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Thumbnail is not a valid JPEG or PNG image") from exc
    digest = hashlib.sha256(data).hexdigest()
    with session_scope() as session:
        row = session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if row is None:
            raise ValueError("publication not found")
        if row.stage != "metadata_hold" or row.status != "queued" or row.youtube_video_id:
            raise ValueError("Thumbnail is locked after upload starts; use YouTube Studio")
        if publication_version(row) != expected_version:
            raise ValueError("Another tab changed this draft. Reload before uploading a thumbnail")
        key = f"manual-thumbnails/{publication_id}/{digest}.{'png' if kind == 'PNG' else 'jpg'}"
        mime = "image/png" if kind == "PNG" else "image/jpeg"
        store = ObjectStore()
        store.ensure_bucket()
        store.put_bytes(data, key, mime)
        row.raw_status = {
            **dict(row.raw_status or {}),
            "manual_plan_version": expected_version + 1,
            "manual_thumbnail": {
                "key": key,
                "sha256": digest,
                "content_type": mime,
                "size_bytes": len(data),
            },
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(row.id),
                event_type="publication.manual_thumbnail_saved",
                payload={"sha256": digest, "actor": actor},
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def reconcile_live_release(publication_id: uuid.UUID, *, actor: str) -> Publication:
    from katcha.integrations.youtube.client import YouTubeClient

    with session_scope() as session:
        row = session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if row is None or not row.youtube_video_id:
            raise ValueError("Publication has no YouTube video to reconcile")
        if row.status not in {"private", "unlisted", "published", "scheduled"}:
            raise ValueError("Wait for processing before reconciling release controls")
        observed = dict(
            YouTubeClient(row.youtube_connection_id)
            .video_resource(row.youtube_video_id)
            .get("status")
            or {}
        )
        privacy = observed.get("privacyStatus")
        if privacy not in {"private", "unlisted", "public"}:
            raise ValueError("YouTube did not return a confirmed visibility")
        value = observed.get("publishAt")
        publish_at = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
        row.privacy_status = "public" if publish_at else privacy
        row.publish_at = publish_at
        row.status = "scheduled" if publish_at else "published" if privacy == "public" else privacy
        row.stage = row.status
        if privacy == "public":
            row.published_at = row.published_at or datetime.now(UTC)
        row.raw_status = {
            **dict(row.raw_status or {}),
            "manual_plan_version": publication_version(row) + 1,
            "release_confirmation": {"state": "confirmed", "observed": observed},
        }
        row.error = None
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(row.id),
                event_type="publication.release_reconciled",
                payload={"actor": actor, "observed": observed},
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row
