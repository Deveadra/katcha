from __future__ import annotations

import uuid

from sqlalchemy import select
from temporalio import activity

from katcha.db import session_scope
from katcha.integrations.storage import ObjectStore
from katcha.models import DomainEvent
from katcha.production_models import Production, ProductionAsset
from katcha.services.render_qc import validate_post_render, validate_pre_render
from katcha.short_episode_models import ShortEpisode, ShortEpisodeAsset


def _event(
    *,
    source_kind: str,
    source_id: str,
    phase: str,
    status: str,
    payload: dict[str, object],
) -> None:
    with session_scope() as session:
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=source_id,
                event_type=f"{source_kind}.render_qc_{status}",
                payload={
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "phase": phase,
                    **payload,
                },
            )
        )


@activity.defn
def pre_render_qc_activity(source_kind: str, source_id: str) -> dict[str, object]:
    source_uuid = uuid.UUID(source_id)
    try:
        with session_scope() as session:
            if source_kind == "production":
                source = session.get(Production, source_uuid)
            elif source_kind == "short_episode":
                source = session.get(ShortEpisode, source_uuid)
            else:
                raise ValueError(f"unsupported render QC source kind: {source_kind}")
            if source is None:
                raise ValueError(f"{source_kind} not found: {source_id}")
            manifest = dict(source.render_manifest or {})
            blueprint = (
                dict(source.edit_blueprint_snapshot)
                if source.edit_blueprint_snapshot is not None
                else None
            )
            brand_key = source.brand_key

        result = validate_pre_render(
            manifest_payload=manifest,
            blueprint_snapshot=blueprint,
            expected_brand_key=brand_key,
            store=ObjectStore(),
        )
    except Exception as exc:
        _event(
            source_kind=source_kind,
            source_id=source_id,
            phase="pre_render",
            status="failed",
            payload={"error": str(exc)[:1000]},
        )
        raise

    payload = result.as_dict()
    _event(
        source_kind=source_kind,
        source_id=source_id,
        phase="pre_render",
        status="passed",
        payload=payload,
    )
    return payload


@activity.defn
def post_render_qc_activity(source_kind: str, source_id: str) -> dict[str, object]:
    source_uuid = uuid.UUID(source_id)
    try:
        with session_scope() as session:
            if source_kind == "production":
                source = session.get(Production, source_uuid)
                asset = session.scalar(
                    select(ProductionAsset).where(
                        ProductionAsset.production_id == source_uuid,
                        ProductionAsset.kind == "render",
                        ProductionAsset.generation == 1,
                    )
                )
            elif source_kind == "short_episode":
                source = session.get(ShortEpisode, source_uuid)
                asset = session.scalar(
                    select(ShortEpisodeAsset).where(
                        ShortEpisodeAsset.short_episode_id == source_uuid,
                        ShortEpisodeAsset.kind == "render",
                        ShortEpisodeAsset.generation == 1,
                    )
                )
            else:
                raise ValueError(f"unsupported render QC source kind: {source_kind}")
            if source is None:
                raise ValueError(f"{source_kind} not found: {source_id}")
            if asset is None:
                raise ValueError("render QC cannot run without a persisted render asset")
            manifest = dict(source.render_manifest or {})
            metadata = dict(asset.asset_metadata or {})
            duration = float(metadata.get("duration_seconds") or 0)
            output_key = asset.storage_key

        result = validate_post_render(
            manifest_payload=manifest,
            output_key=output_key,
            actual_duration_seconds=duration,
            store=ObjectStore(),
        )
    except Exception as exc:
        _event(
            source_kind=source_kind,
            source_id=source_id,
            phase="post_render",
            status="failed",
            payload={"error": str(exc)[:1000]},
        )
        raise

    payload = result.as_dict()
    with session_scope() as session:
        if source_kind == "production":
            asset = session.scalar(
                select(ProductionAsset).where(
                    ProductionAsset.production_id == source_uuid,
                    ProductionAsset.kind == "render",
                    ProductionAsset.generation == 1,
                )
            )
        else:
            asset = session.scalar(
                select(ShortEpisodeAsset).where(
                    ShortEpisodeAsset.short_episode_id == source_uuid,
                    ShortEpisodeAsset.kind == "render",
                    ShortEpisodeAsset.generation == 1,
                )
            )
        if asset is None:
            raise RuntimeError("render asset disappeared while persisting QC")
        asset.asset_metadata = {
            **dict(asset.asset_metadata or {}),
            "qc": payload,
        }
        session.add(
            DomainEvent(
                aggregate_type=source_kind,
                aggregate_id=source_id,
                event_type=f"{source_kind}.render_qc_passed",
                payload={
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "phase": "post_render",
                    **payload,
                },
            )
        )
    return payload
