from __future__ import annotations

import uuid

from sqlalchemy import select
from temporalio import activity

from katcha.db import session_scope
from katcha.models import Clip, DomainEvent
from katcha.rendering.client import render_ranked_episode
from katcha.rendering.manifest import ShortBrandSpec
from katcha.rendering.ranked_episode_manifest import (
    RankedEpisodeRenderManifest,
    build_ranked_episode_manifest,
)
from katcha.rendering.reactions import ReactionAssetPack
from katcha.services.acquisition import assert_clip_production_eligible
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
        result.append({**metadata, "storage_key": asset.storage_key})
    if not result:
        raise RuntimeError("short episode has no narration assets")
    return result


@activity.defn
def build_ranked_episode_render_manifest_activity(
    episode_id: str,
    render_generation: int = 1,
) -> dict[str, object]:
    episode_uuid = uuid.UUID(episode_id)
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
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
        if render_generation < 1:
            raise ValueError("render_generation must be positive")
        output_key = f"short-episodes/{episode_id}/renders/a{render_generation}.mp4"
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
                    "render_generation": render_generation,
                },
            )
        )
        return {
            "episode_id": episode_id,
            "output_key": manifest.output_key,
            "duration_seconds": manifest.output_duration_seconds,
            "reused": False,
            "render_generation": render_generation,
        }


@activity.defn
def render_ranked_episode_activity(
    episode_id: str,
    render_generation: int = 1,
) -> dict[str, object]:
    episode_uuid = uuid.UUID(episode_id)
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        existing = session.scalar(
            select(ShortEpisodeAsset).where(
                ShortEpisodeAsset.short_episode_id == episode_uuid,
                ShortEpisodeAsset.kind == "render",
                ShortEpisodeAsset.generation == render_generation,
            )
        )
        if existing is not None:
            episode.status = "rendering"
            episode.stage = "render_output_ready"
            return {
                "episode_id": episode_id,
                "output_key": existing.storage_key,
                "reused": True,
            }
        if not episode.render_manifest:
            raise RuntimeError("short episode has no frozen render manifest")
        manifest = RankedEpisodeRenderManifest.model_validate(episode.render_manifest)

    result = render_ranked_episode(manifest)

    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise RuntimeError("short episode disappeared after render")
        existing = session.scalar(
            select(ShortEpisodeAsset).where(
                ShortEpisodeAsset.short_episode_id == episode_uuid,
                ShortEpisodeAsset.kind == "render",
                ShortEpisodeAsset.generation == render_generation,
            )
        )
        if existing is None:
            session.add(
                ShortEpisodeAsset(
                    short_episode_id=episode_uuid,
                    kind="render",
                    generation=render_generation,
                    storage_key=result.output_key,
                    content_type="video/mp4",
                    provider="remotion",
                    model="ranked-episode-render-v1",
                    asset_metadata={
                        **dict(result.metadata or {}),
                        "duration_seconds": result.duration_seconds,
                        "treatment": manifest.treatment.model_dump(mode="json"),
                    "render_generation": render_generation,
                    },
                )
            )
        episode.status = "rendering"
        episode.stage = "render_output_ready"
        episode.error = None
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=episode_id,
                event_type="short_episode.rendered",
                payload={
                    "short_episode_id": episode_id,
                    "output_key": result.output_key,
                    "duration_seconds": result.duration_seconds,
                    "treatment": manifest.treatment.model_dump(mode="json"),
                },
            )
        )
    return {
        "episode_id": episode_id,
        "output_key": result.output_key,
        "duration_seconds": result.duration_seconds,
        "reused": False,
        "render_generation": render_generation,
    }
