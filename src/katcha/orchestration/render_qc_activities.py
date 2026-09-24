from __future__ import annotations

import uuid

from sqlalchemy import select
from temporalio import activity

from katcha.db import session_scope
from katcha.edit_render_models import RenderAttempt
from katcha.integrations.storage import ObjectStore
from katcha.production_models import Production, ProductionAsset
from katcha.services.render_qc import validate_post_render, validate_pre_render
from katcha.services.render_recovery import (
    begin_render_attempt,
    bind_render_manifest,
    dead_letter_render_attempt,
    record_post_render_success,
    record_pre_render_qc,
    register_render_attempt,
)
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset


def _source(
    source_kind: str,
    source_id: uuid.UUID,
) -> Production | ShortEpisode:
    with session_scope() as session:
        if source_kind == "production":
            row = session.get(Production, source_id)
        elif source_kind == "short_episode":
            row = session.get(ShortEpisode, source_id)
        else:
            raise ValueError(f"unsupported render source kind: {source_kind}")
        if row is None:
            raise ValueError(f"{source_kind.replace('_', ' ')} not found: {source_id}")
        session.expunge(row)
        return row


@activity.defn
def begin_render_attempt_activity(
    source_kind: str,
    source_id: str,
    workflow_id: str,
) -> dict[str, object]:
    row = register_render_attempt(
        source_kind,  # type: ignore[arg-type]
        uuid.UUID(source_id),
        workflow_id=workflow_id,
        requested_by="workflow",
    )
    row = begin_render_attempt(row.id)
    return {
        "render_attempt_id": str(row.id),
        "attempt_number": row.attempt_number,
        "workflow_id": row.workflow_id,
        "status": row.status,
    }


@activity.defn
def pre_render_qc_activity(
    source_kind: str,
    source_id: str,
    render_attempt_id: str,
) -> dict[str, object]:
    source_uuid = uuid.UUID(source_id)
    attempt_uuid = uuid.UUID(render_attempt_id)
    source = _source(source_kind, source_uuid)
    manifest = dict(source.render_manifest or {})
    bind_render_manifest(attempt_uuid, manifest)
    result = validate_pre_render(
        manifest_payload=manifest,
        blueprint_snapshot=(
            dict(source.edit_blueprint_snapshot)
            if source.edit_blueprint_snapshot is not None
            else None
        ),
        expected_brand_key=source.brand_key,
        store=ObjectStore(),
    )
    payload = result.as_dict()
    record_pre_render_qc(attempt_uuid, payload)
    return payload


@activity.defn
def post_render_qc_activity(
    source_kind: str,
    source_id: str,
    render_attempt_id: str,
) -> dict[str, object]:
    source_uuid = uuid.UUID(source_id)
    attempt_uuid = uuid.UUID(render_attempt_id)
    with session_scope() as session:
        attempt = session.get(RenderAttempt, attempt_uuid)
        if attempt is None:
            raise ValueError(f"render attempt not found: {render_attempt_id}")
        generation = attempt.attempt_number
        manifest = dict(attempt.manifest_snapshot or {})
        if source_kind == "production":
            asset = session.scalar(
                select(ProductionAsset).where(
                    ProductionAsset.production_id == source_uuid,
                    ProductionAsset.kind == "render",
                    ProductionAsset.generation == generation,
                )
            )
        elif source_kind == "short_episode":
            asset = session.scalar(
                select(ShortEpisodeAsset).where(
                    ShortEpisodeAsset.short_episode_id == source_uuid,
                    ShortEpisodeAsset.kind == "render",
                    ShortEpisodeAsset.generation == generation,
                )
            )
        else:
            raise ValueError(f"unsupported render source kind: {source_kind}")
        if asset is None:
            raise ValueError("post-render QC cannot find the render asset for this attempt")
        output_key = asset.storage_key
        metadata = dict(asset.asset_metadata or {})
        duration = float(metadata.get("duration_seconds") or 0)

    result = validate_post_render(
        manifest_payload=manifest,
        output_key=output_key,
        actual_duration_seconds=duration,
        store=ObjectStore(),
    )
    payload = result.as_dict()
    with session_scope() as session:
        if source_kind == "production":
            asset = session.scalar(
                select(ProductionAsset).where(
                    ProductionAsset.production_id == source_uuid,
                    ProductionAsset.kind == "render",
                    ProductionAsset.generation == generation,
                )
            )
        else:
            asset = session.scalar(
                select(ShortEpisodeAsset).where(
                    ShortEpisodeAsset.short_episode_id == source_uuid,
                    ShortEpisodeAsset.kind == "render",
                    ShortEpisodeAsset.generation == generation,
                )
            )
        if asset is None:
            raise RuntimeError("render asset disappeared while persisting QC")
        asset.asset_metadata = {
            **dict(asset.asset_metadata or {}),
            "qc": payload,
            "render_attempt_id": render_attempt_id,
            "render_attempt_number": generation,
        }
    record_post_render_success(attempt_uuid, payload, output_key=output_key)
    return payload


@activity.defn
def dead_letter_render_attempt_activity(
    render_attempt_id: str,
    message: str,
) -> None:
    dead_letter_render_attempt(
        uuid.UUID(render_attempt_id),
        message,
        failure_kind="workflow_exhausted",
    )
