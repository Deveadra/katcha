from __future__ import annotations

import uuid

from sqlalchemy import select
from temporalio import activity

from katcha.db import session_scope
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent
from katcha.rendering.client import render_ranked_episode
from katcha.rendering.manifest import ShortBrandSpec
from katcha.rendering.ranked_episode_manifest import (
    RankedEpisodeRenderManifest,
    build_ranked_episode_manifest,
)
from katcha.rendering.reactions import ReactionAssetPack
from katcha.services.acquisition import assert_clip_production_eligible
from katcha.services.render_recovery import (
    ensure_render_attempt,
    mark_render_attempt_retryable_failure,
    mark_render_attempt_started,
    mark_render_attempt_verified,
)
from katcha.short_episode_models import (
    ShortEpisode,
    ShortEpisodeAsset,
    ShortEpisodeItem,
    ShortEpisodeScript,
)


def _selected_script(session: object, episode: ShortEpisode) -> ShortEpisodeScript:
    if episode.selected_script_id is None:
        raise RuntimeError("short episode has no selected script")
    script = session.get(ShortEpisodeScript, episode.selected_script_id)
    if script is None:
        raise RuntimeError("selected short episode script does not exist")
    return script


def _ordered_render_items(session: object, episode: ShortEpisode) -> list[dict[str, object]]:
    items = list(
        session.scalars(
            select(ShortEpisodeItem)
            .where(ShortEpisodeItem.short_episode_id == episode.id)
            .order_by(ShortEpisodeItem.position.desc())
        )
    )
    if len(items) != episode.item_count:
        raise RuntimeError("short episode item plan is incomplete")

    result: list[dict[str, object]] = []
    for item in items:
        acquisition = assert_clip_production_eligible(item.clip_id)
        if not acquisition.eligible:
            raise RuntimeError(f"clip is no longer production eligible: {item.clip_id}")
        clip = session.get(Clip, item.clip_id)
        if clip is None:
            raise RuntimeError(f"short episode clip disappeared: {item.clip_id}")
        store = ObjectStore()
        store.ensure_bucket()
        if not store.exists(clip.storage_key):
            raise RuntimeError(f"short episode source object is missing: {item.clip_id}")
        duration = float(clip.duration_seconds or 0)
        if duration <= 0:
            raise RuntimeError(f"short episode clip has no usable duration: {item.clip_id}")
        result.append(
            {
                "position": item.position,
                "role": item.role,
                "clip_id": str(item.clip_id),
                "storage_key": clip.storage_key,
                "source_duration_seconds": duration,
                "width": clip.width,
                "height": clip.height,
                "native_audio_policy": "duck",
                "audio_volume": 0.35,
                "narration_duck_volume": 0.16,
            }
        )
    return result


def _narration_assets(session: object, episode: ShortEpisode) -> list[dict[str, object]]:
    assets = list(
        session.scalars(
            select(ShortEpisodeAsset)
            .where(
                ShortEpisodeAsset.short_episode_id == episode.id,
                ShortEpisodeAsset.kind.like("narration_%"),
                ShortEpisodeAsset.generation == 1,
            )
            .order_by(ShortEpisodeAsset.kind)
        )
    )
    result: list[dict[str, object]] = []
    for asset in assets:
        metadata = dict(asset.asset_metadata or {})
        if metadata.get("sequence") is None:
            raise RuntimeError(f"narration asset is missing sequence metadata: {asset.kind}")
        store = ObjectStore()
        store.ensure_bucket()
        if not store.exists(asset.storage_key):
            raise RuntimeError(f"narration object is missing: {asset.kind}")
        result.append({**metadata, "storage_key": asset.storage_key})
    if not result:
        raise RuntimeError("short episode has no narration assets")
    return result


@activity.defn
def build_ranked_episode_render_manifest_activity(episode_id: str) -> dict[str, object]:
    episode_uuid = uuid.UUID(episode_id)
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        if episode.render_manifest:
            manifest = RankedEpisodeRenderManifest.model_validate(episode.render_manifest)
            return {
                "episode_id": episode_id,
                "output_key": manifest.output_key,
                "duration_seconds": manifest.output_duration_seconds,
                "reused": True,
            }
        if episode.status != "editorial_approved":
            raise RuntimeError("short episode must pass editorial approval before rendering")

        script = _selected_script(session, episode)
        ordered_items = _ordered_render_items(session, episode)
        narration_assets = _narration_assets(session, episode)
        visual = dict((episode.brand_snapshot or {}).get("visual") or {})
        brand = ShortBrandSpec.model_validate(visual)
        reaction_pack_payload = visual.get("reaction_pack")
        reaction_pack = (
            ReactionAssetPack.model_validate(reaction_pack_payload)
            if reaction_pack_payload is not None
            else None
        )
        script_payload = dict(script.script_payload or {})
        interaction_prompt = script_payload.get("interaction_prompt")
        output_key = f"short-episodes/{episode_id}/renders/g1.mp4"
        manifest = build_ranked_episode_manifest(
            short_episode_id=episode_id,
            premise=episode.premise,
            format_key=episode.format_key,
            format_version=episode.format_version,
            ordered_items=ordered_items,
            narration_assets=narration_assets,
            selected_style=script.style,
            interaction_prompt=(
                str(interaction_prompt).strip() if interaction_prompt else None
            ),
            output_key=output_key,
            brand=brand,
            reaction_pack=reaction_pack,
            reaction_cues=list(script_payload.get("reaction_cues") or []),
            trend_opportunity_id=(
                str(episode.trend_opportunity_id) if episode.trend_opportunity_id else None
            ),
        )
        episode.render_manifest = manifest.model_dump(mode="json")
        episode.status = "rendering"
        episode.stage = "render_manifest_ready"
        episode.error = None
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=episode_id,
                event_type="short_episode.render_manifest_ready",
                payload={
                    "short_episode_id": episode_id,
                    "output_key": manifest.output_key,
                    "duration_seconds": manifest.output_duration_seconds,
                    "treatment": manifest.treatment.model_dump(mode="json"),
                },
            )
        )
        return {
            "episode_id": episode_id,
            "output_key": manifest.output_key,
            "duration_seconds": manifest.output_duration_seconds,
            "reused": False,
        }


@activity.defn
def render_ranked_episode_activity(episode_id: str) -> dict[str, object]:
    episode_uuid = uuid.UUID(episode_id)
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        if not episode.render_manifest:
            raise RuntimeError("short episode has no frozen render manifest")
        manifest = RankedEpisodeRenderManifest.model_validate(episode.render_manifest)
        if episode.edit_blueprint_key and episode.edit_blueprint_snapshot:
            frozen_key = str(episode.edit_blueprint_snapshot.get("key") or "")
            if frozen_key != episode.edit_blueprint_key:
                raise RuntimeError("short episode edit blueprint lineage is stale")
        existing = session.scalar(
            select(ShortEpisodeAsset).where(
                ShortEpisodeAsset.short_episode_id == episode_uuid,
                ShortEpisodeAsset.kind == "render",
                ShortEpisodeAsset.generation == 1,
            )
        )
        existing_metadata = dict(existing.asset_metadata or {}) if existing else {}
        existing_key = existing.storage_key if existing is not None else None
        episode.status = "rendering"
        episode.stage = "rendering"

    attempt = ensure_render_attempt(
        "short_episode",
        episode_uuid,
        output_key=manifest.output_key,
        manifest_version=manifest.version,
    )
    if existing_key is not None:
        if not bool(existing_metadata.get("verified")):
            raise RuntimeError("existing short episode render is not post-render verified")
        mark_render_attempt_verified(attempt.id, verification=existing_metadata)
        with session_scope() as session:
            episode = session.get(ShortEpisode, episode_uuid)
            if episode is not None:
                episode.status = "rendered"
                episode.stage = "render_verified"
                episode.error = None
        return {
            "episode_id": episode_id,
            "output_key": existing_key,
            "reused": True,
            "verified": True,
            "render_attempt_id": str(attempt.id),
        }

    mark_render_attempt_started(attempt.id)
    try:
        result = render_ranked_episode(manifest)
        if not bool((result.metadata or {}).get("verified")):
            raise RuntimeError("renderer output failed post-render verification")
    except Exception as exc:
        mark_render_attempt_retryable_failure(
            attempt.id,
            failure_class=type(exc).__name__,
            error=str(exc),
        )
        raise

    verification = {
        **dict(result.metadata or {}),
        "duration_seconds": result.duration_seconds,
        "output_key": result.output_key,
    }
    mark_render_attempt_verified(attempt.id, verification=verification)

    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise RuntimeError("short episode disappeared after render")
        existing = session.scalar(
            select(ShortEpisodeAsset).where(
                ShortEpisodeAsset.short_episode_id == episode_uuid,
                ShortEpisodeAsset.kind == "render",
                ShortEpisodeAsset.generation == 1,
            )
        )
        if existing is None:
            session.add(
                ShortEpisodeAsset(
                    short_episode_id=episode_uuid,
                    kind="render",
                    generation=1,
                    storage_key=result.output_key,
                    content_type="video/mp4",
                    provider="remotion",
                    model="ranked-episode-render-v1",
                    asset_metadata={
                        **dict(result.metadata or {}),
                        "duration_seconds": result.duration_seconds,
                        "render_attempt_id": str(attempt.id),
                        "edit_blueprint_key": episode.edit_blueprint_key,
                        "edit_blueprint_version": episode.edit_blueprint_version,
                        "treatment": manifest.treatment.model_dump(mode="json"),
                    },
                )
            )
        episode.status = "rendered"
        episode.stage = "render_verified"
        episode.error = None
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=episode_id,
                event_type="short_episode.render_verified",
                payload={
                    "short_episode_id": episode_id,
                    "render_attempt_id": str(attempt.id),
                    "output_key": result.output_key,
                    "duration_seconds": result.duration_seconds,
                    "verification": result.metadata,
                    "treatment": manifest.treatment.model_dump(mode="json"),
                },
            )
        )
    return {
        "episode_id": episode_id,
        "output_key": result.output_key,
        "duration_seconds": result.duration_seconds,
        "reused": False,
        "verified": True,
        "render_attempt_id": str(attempt.id),
    }

