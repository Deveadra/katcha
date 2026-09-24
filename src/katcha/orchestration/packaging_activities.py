from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from temporalio import activity

from katcha.db import session_scope
from katcha.domain import AutomationLevel, ChannelStatus, YouTubeConnectionStatus
from katcha.integrations.storage import ObjectStore
from katcha.integrations.youtube.client import YouTubeClient
from katcha.intelligence_models import AutomationPolicyVersion, ChannelProfile
from katcha.models import DomainEvent
from katcha.packaging_models import (
    PackagingExperiment,
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication, YouTubeConnection


def _require_automatic_experiment_policy(
    activation: PublicationPackagingActivation,
) -> None:
    if not activation.activation_key.startswith("experiment-"):
        return
    rollback = activation.activation_key.startswith("experiment-rollback-")
    suffix = activation.activation_key.removeprefix(
        "experiment-rollback-" if rollback else "experiment-"
    )
    try:
        experiment_id = uuid.UUID(suffix)
    except ValueError as exc:
        raise RuntimeError("automatic packaging activation has invalid lineage") from exc
    with session_scope() as session:
        experiment = session.get(PackagingExperiment, experiment_id)
        if (
            experiment is None
            or experiment.publication_id != activation.publication_id
            or activation.variant_id
            != (experiment.previous_variant_id if rollback else experiment.candidate_variant_id)
            or experiment.status
            not in ({"rollback_pending"} if rollback else {"pending", "preparing"})
        ):
            raise RuntimeError("automatic packaging activation lineage is invalid")
        profile = session.get(ChannelProfile, experiment.channel_profile_id)
        if profile is None or profile.status != ChannelStatus.ACTIVE.value:
            raise RuntimeError("channel no longer permits automatic packaging")
        policy = session.scalar(
            select(AutomationPolicyVersion).where(
                AutomationPolicyVersion.channel_profile_id == profile.id,
                AutomationPolicyVersion.version == profile.active_automation_version,
            )
        )
        if policy is None or policy.level != AutomationLevel.AUTO_PUBLISH_SCHEDULED.value:
            raise RuntimeError("channel no longer permits automatic packaging")


def _activation_bundle(
    activation_id: uuid.UUID,
) -> tuple[PublicationPackagingActivation, Publication, PublicationPackagingVariant]:
    with session_scope() as session:
        activation = session.get(PublicationPackagingActivation, activation_id)
        if activation is None:
            raise ValueError(f"packaging activation not found: {activation_id}")
        publication = session.get(Publication, activation.publication_id)
        if publication is None:
            raise RuntimeError("packaging activation publication disappeared")
        variant = session.get(PublicationPackagingVariant, activation.variant_id)
        if variant is None:
            raise RuntimeError("packaging activation variant disappeared")
        if variant.publication_id != publication.id:
            raise RuntimeError("packaging activation variant/publication mismatch")
        session.expunge(activation)
        session.expunge(publication)
        session.expunge(variant)
        return activation, publication, variant


@activity.defn
def prepare_packaging_activation_activity(activation_id: str) -> dict[str, object]:
    activation_uuid = uuid.UUID(activation_id)
    with session_scope() as session:
        activation = session.get(PublicationPackagingActivation, activation_uuid)
        if activation is None:
            raise ValueError(f"packaging activation not found: {activation_id}")
        if activation.status == "applied":
            return {
                "activation_id": activation_id,
                "done": True,
                "stage": activation.stage,
            }
        if activation.status == "failed":
            raise ValueError("failed packaging activation requires a new activation key")
        publication = session.get(Publication, activation.publication_id)
        if publication is None:
            raise RuntimeError("packaging activation publication disappeared")
        if not publication.youtube_video_id:
            raise RuntimeError("publication no longer has a YouTube video ID")
        if publication.youtube_video_id != activation.youtube_video_id:
            raise RuntimeError("publication YouTube video ID changed after activation registration")
        connection = session.get(YouTubeConnection, publication.youtube_connection_id)
        if connection is None or connection.status != YouTubeConnectionStatus.ACTIVE.value:
            raise RuntimeError("publication YouTube connection is not active")
        variant = session.get(PublicationPackagingVariant, activation.variant_id)
        if variant is None or variant.publication_id != publication.id:
            raise RuntimeError("packaging activation variant/publication mismatch")
        _require_automatic_experiment_policy(activation)
        activation.status = "running"
        if activation.stage == "queued":
            activation.stage = "prepared"
        activation.error = None
        return {
            "activation_id": activation_id,
            "done": False,
            "stage": activation.stage,
            "has_thumbnail": variant.thumbnail_storage_key is not None,
        }


@activity.defn
def apply_packaging_text_activity(activation_id: str) -> dict[str, object]:
    activation_uuid = uuid.UUID(activation_id)
    activation, publication, variant = _activation_bundle(activation_uuid)
    if activation.status == "applied" or activation.stage in {
        "text_applied",
        "thumbnail_applied",
        "thumbnail_skipped",
        "completed",
    }:
        return {"activation_id": activation_id, "reused": True, "stage": activation.stage}
    if activation.status != "running":
        raise RuntimeError("packaging activation is not running")
    _require_automatic_experiment_policy(activation)

    if publication.title != variant.title or publication.description != variant.description:
        client = YouTubeClient(publication.youtube_connection_id)
        client.update_snippet(
            activation.youtube_video_id,
            title=variant.title,
            description=variant.description,
            tags=list(publication.tags or []),
            category_id=publication.category_id,
        )

    with session_scope() as session:
        stored = session.get(PublicationPackagingActivation, activation_uuid)
        publication_row = session.get(Publication, publication.id)
        if stored is None or publication_row is None:
            raise RuntimeError("packaging activation disappeared after snippet update")
        publication_row.title = variant.title
        publication_row.description = variant.description
        stored.stage = "text_applied"
        stored.activation_metadata = {
            **dict(stored.activation_metadata or {}),
            "text_applied": True,
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication.id),
                event_type="publication.packaging_text_applied",
                payload={
                    "publication_id": str(publication.id),
                    "packaging_activation_id": activation_id,
                    "packaging_variant_id": str(variant.id),
                    "youtube_video_id": activation.youtube_video_id,
                },
            )
        )
    return {"activation_id": activation_id, "reused": False, "stage": "text_applied"}


@activity.defn
def apply_packaging_thumbnail_activity(activation_id: str) -> dict[str, object]:
    activation_uuid = uuid.UUID(activation_id)
    activation, publication, variant = _activation_bundle(activation_uuid)
    if activation.status == "applied" or activation.stage in {
        "thumbnail_applied",
        "thumbnail_skipped",
        "completed",
    }:
        return {"activation_id": activation_id, "reused": True, "stage": activation.stage}
    if activation.stage != "text_applied":
        raise RuntimeError("packaging text stage must complete before thumbnail activation")
    if variant.thumbnail_storage_key is None:
        with session_scope() as session:
            stored = session.get(PublicationPackagingActivation, activation_uuid)
            if stored is None:
                raise RuntimeError("packaging activation disappeared before thumbnail skip")
            stored.stage = "thumbnail_skipped"
        return {
            "activation_id": activation_id,
            "reused": False,
            "stage": "thumbnail_skipped",
        }

    _require_automatic_experiment_policy(activation)
    store = ObjectStore()
    data = store.get_bytes(variant.thumbnail_storage_key)
    if len(data) != int(variant.thumbnail_size_bytes or 0):
        raise RuntimeError("packaging thumbnail size no longer matches frozen variant")
    if hashlib.sha256(data).hexdigest() != variant.thumbnail_sha256:
        raise RuntimeError("packaging thumbnail hash no longer matches frozen variant")

    client = YouTubeClient(publication.youtube_connection_id)
    response = client.set_thumbnail(
        activation.youtube_video_id,
        data=data,
        mime_type=str(variant.thumbnail_content_type),
    )
    with session_scope() as session:
        stored = session.get(PublicationPackagingActivation, activation_uuid)
        if stored is None:
            raise RuntimeError("packaging activation disappeared after thumbnail update")
        stored.stage = "thumbnail_applied"
        stored.activation_metadata = {
            **dict(stored.activation_metadata or {}),
            "thumbnail_applied": True,
            "thumbnail_response_kind": response.get("kind"),
            "thumbnail_response_etag": response.get("etag"),
        }
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication.id),
                event_type="publication.packaging_thumbnail_applied",
                payload={
                    "publication_id": str(publication.id),
                    "packaging_activation_id": activation_id,
                    "packaging_variant_id": str(variant.id),
                    "youtube_video_id": activation.youtube_video_id,
                    "thumbnail_sha256": variant.thumbnail_sha256,
                },
            )
        )
    return {"activation_id": activation_id, "reused": False, "stage": "thumbnail_applied"}


@activity.defn
def finalize_packaging_activation_activity(activation_id: str) -> dict[str, object]:
    activation_uuid = uuid.UUID(activation_id)
    now = datetime.now(UTC)
    with session_scope() as session:
        activation = session.get(PublicationPackagingActivation, activation_uuid)
        if activation is None:
            raise ValueError(f"packaging activation not found: {activation_id}")
        if activation.status == "applied":
            return {
                "activation_id": activation_id,
                "status": "applied",
                "reused": True,
            }
        if activation.stage not in {"thumbnail_applied", "thumbnail_skipped"}:
            raise RuntimeError("packaging activation is not ready to finalize")
        publication = session.get(Publication, activation.publication_id)
        variant = session.get(PublicationPackagingVariant, activation.variant_id)
        if publication is None or variant is None:
            raise RuntimeError("packaging activation lineage disappeared")
        packaging_lineage = {
            "activation_id": activation_id,
            "variant_id": str(variant.id),
            "variant_key": variant.variant_key,
            "version": variant.version,
            "title": variant.title,
            "thumbnail_storage_key": variant.thumbnail_storage_key,
            "thumbnail_sha256": variant.thumbnail_sha256,
            "activated_at": now.isoformat(),
        }
        publication.treatment_metadata = {
            **dict(publication.treatment_metadata or {}),
            "active_packaging": packaging_lineage,
        }
        activation.status = "applied"
        activation.stage = "completed"
        activation.applied_at = now
        activation.error = None
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(publication.id),
                event_type="publication.packaging_activated",
                payload={
                    "publication_id": str(publication.id),
                    "packaging_activation_id": activation_id,
                    "packaging_variant_id": str(variant.id),
                    "variant_key": variant.variant_key,
                    "version": variant.version,
                    "youtube_video_id": activation.youtube_video_id,
                    "thumbnail_sha256": variant.thumbnail_sha256,
                },
            )
        )
    return {"activation_id": activation_id, "status": "applied", "reused": False}


@activity.defn
def mark_packaging_activation_failed(activation_id: str, message: str) -> None:
    activation_uuid = uuid.UUID(activation_id)
    with session_scope() as session:
        activation = session.get(PublicationPackagingActivation, activation_uuid)
        if activation is None or activation.status == "applied":
            return
        activation.status = "failed"
        activation.error = message[:8000]
        session.add(
            DomainEvent(
                aggregate_type="publication",
                aggregate_id=str(activation.publication_id),
                event_type="publication.packaging_activation_failed",
                payload={
                    "publication_id": str(activation.publication_id),
                    "packaging_activation_id": activation_id,
                    "packaging_variant_id": str(activation.variant_id),
                    "stage": activation.stage,
                    "error": message[:1000],
                },
            )
        )
