from __future__ import annotations

import uuid
from typing import Any

from katcha.audio.captions import build_caption_cues
from katcha.db import session_scope
from katcha.models import Clip, DomainEvent
from katcha.rendering.manifest import RenderCaptionCue, ShortBrandSpec
from katcha.rendering.ranked_episode_manifest import (
    MAX_RANKED_EPISODE_SECONDS,
    RankedEpisodeNarrationOverlay,
    RankedEpisodeRenderManifest,
)
from katcha.services.channel_brands import brand_for_channel
from katcha.services.short_episode_reviews import register_short_episode_regeneration
from katcha.short_episode_models import ShortEpisode


def _overlay_start(
    placement: str,
    position: int | None,
    duration: float,
    items: list[Any],
    item_timeline_end: float,
    narration_cursor: float,
) -> float:
    item = next((value for value in items if value.position == position), None)
    item_start = item.timeline_start_seconds if item is not None else 0.0
    item_end = item.timeline_end_seconds if item is not None else item_timeline_end
    if placement == "opening":
        requested = 0.08
    elif placement in {"reveal", "pre_clip"}:
        requested = item_start + 0.08
    elif placement == "post_clip":
        requested = max(item_start + 0.1, item_end - duration - 0.08)
    elif placement == "transition":
        requested = max(item_start + 0.1, item_end - duration - 0.04)
    elif placement == "closing":
        requested = item_timeline_end + 0.05
    elif placement == "interaction":
        requested = max(item_timeline_end + 0.05, narration_cursor)
    else:
        raise ValueError(f"unsupported episode narration placement: {placement}")
    return max(requested, narration_cursor)


def _prepare_manifest(
    episode_id: uuid.UUID,
    edits: list[dict[str, object]],
    *,
    adopt_active_brand: bool,
) -> tuple[RankedEpisodeRenderManifest, dict[str, object] | None, int | None]:
    positions = [int(edit["position"]) for edit in edits]
    if len(positions) != len(set(positions)):
        raise ValueError("studio clip edits must target unique countdown positions")

    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_id)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        if not episode.render_manifest:
            raise ValueError("episode has no frozen render manifest to edit")
        manifest = RankedEpisodeRenderManifest.model_validate(episode.render_manifest)
        edit_by_position = {int(edit["position"]): edit for edit in edits}

        next_items = []
        cursor = 0.0
        for item in manifest.items:
            edit = edit_by_position.get(item.position)
            source = item.source
            if edit is not None:
                clip = session.get(Clip, uuid.UUID(source.clip_id))
                if clip is None:
                    raise ValueError(f"source clip not found: {source.clip_id}")
                source_start = float(edit.get("source_start_seconds", source.source_start_seconds))
                duration = float(edit.get("duration_seconds", source.duration_seconds))
                if duration < 0.5:
                    raise ValueError(f"clip #{item.position} must remain at least 0.5 seconds")
                source_end = source_start + duration
                source_duration = float(clip.duration_seconds or 0)
                if source_end > source_duration + 0.01:
                    raise ValueError(
                        f"clip #{item.position} trim ends at {source_end:.2f}s "
                        f"but source duration is {source_duration:.2f}s"
                    )
                source = source.model_copy(
                    update={
                        "source_start_seconds": round(source_start, 3),
                        "source_end_seconds": round(source_end, 3),
                        "duration_seconds": round(duration, 3),
                        "native_audio_policy": str(
                            edit.get("native_audio_policy", source.native_audio_policy)
                        ),
                        "audio_volume": float(edit.get("audio_volume", source.audio_volume)),
                        "narration_duck_volume": float(
                            edit.get("narration_duck_volume", source.narration_duck_volume)
                        ),
                    }
                )
                transition = str(edit.get("transition_before", item.transition_before))
            else:
                duration = source.duration_seconds
                transition = item.transition_before
            next_items.append(
                item.model_copy(
                    update={
                        "source": source,
                        "timeline_start_seconds": round(cursor, 3),
                        "timeline_end_seconds": round(cursor + float(duration), 3),
                        "transition_before": transition,
                    }
                )
            )
            cursor += float(duration)

        unknown = set(edit_by_position) - {item.position for item in manifest.items}
        if unknown:
            raise ValueError(f"studio edit references unknown countdown position: {min(unknown)}")

        narration_cursor = 0.08
        narration_seconds = 0.0
        next_overlays: list[RankedEpisodeNarrationOverlay] = []
        for overlay in sorted(manifest.overlays, key=lambda value: value.sequence):
            start = _overlay_start(
                overlay.placement,
                overlay.position,
                overlay.duration_seconds,
                next_items,
                cursor,
                narration_cursor,
            )
            cues = [
                RenderCaptionCue(
                    start_seconds=cue.start_seconds,
                    end_seconds=cue.end_seconds,
                    text=cue.text,
                )
                for cue in build_caption_cues(
                    overlay.text,
                    duration_seconds=overlay.duration_seconds,
                    start_offset_seconds=start,
                )
            ]
            next_overlays.append(
                overlay.model_copy(
                    update={"start_seconds": round(start, 3), "cues": cues}
                )
            )
            narration_cursor = start + overlay.duration_seconds + 0.06
            narration_seconds += overlay.duration_seconds

        interaction = next(
            (overlay for overlay in next_overlays if overlay.placement == "interaction"),
            None,
        )
        if interaction is not None:
            end_card_start = interaction.start_seconds
            end_card_duration = max(2.4, interaction.duration_seconds + 0.35)
        elif manifest.end_card.prompt:
            end_card_start = max(cursor, narration_cursor) + 0.05
            end_card_duration = max(2.4, manifest.end_card.duration_seconds)
        else:
            end_card_start = max(cursor, narration_cursor)
            end_card_duration = 0.35

        output_duration = max(
            cursor,
            narration_cursor,
            end_card_start + end_card_duration,
        ) + 0.1
        if output_duration > MAX_RANKED_EPISODE_SECONDS:
            raise ValueError(
                "studio edit exceeds the 60-second channel format; shorten one or more clips"
            )

        overlay_by_sequence = {overlay.sequence: overlay for overlay in next_overlays}
        reactions = []
        for event in manifest.reaction_events:
            overlay = overlay_by_sequence.get(event.line_ref)
            if overlay is None:
                raise ValueError(f"reaction references missing narration line: {event.line_ref}")
            start = round(overlay.start_seconds + event.offset_seconds, 3)
            if start + event.duration_seconds > output_duration + 0.00001:
                raise ValueError(f"reaction extends beyond edited render: {event.id}")
            reactions.append(event.model_copy(update={"start_seconds": start}))

        brand_snapshot: dict[str, object] | None = None
        brand_version: int | None = None
        brand = manifest.brand
        if adopt_active_brand:
            active_contract, brand_version = brand_for_channel(
                session, episode.channel_profile_id
            )
            brand_snapshot = active_contract.model_dump(mode="json")
            brand = ShortBrandSpec.model_validate(active_contract.visual)
            if any(event.brand_key != brand.brand_key for event in reactions):
                reactions = []

        treatment = manifest.treatment.model_copy(
            update={
                "narration_density": round(
                    narration_seconds / output_duration if output_duration else 0.0,
                    6,
                ),
                "brand_key": brand.brand_key,
                "brand_version": brand.version,
            }
        )
        prepared = manifest.model_copy(
            update={
                "items": next_items,
                "overlays": next_overlays,
                "end_card": manifest.end_card.model_copy(
                    update={
                        "start_seconds": round(end_card_start, 3),
                        "duration_seconds": round(end_card_duration, 3),
                    }
                ),
                "output_duration_seconds": round(output_duration, 3),
                "brand": brand,
                "treatment": treatment,
                "reaction_events": reactions,
            }
        )
        return (
            RankedEpisodeRenderManifest.model_validate(prepared.model_dump(mode="json")),
            brand_snapshot,
            brand_version,
        )


def create_studio_render_generation(
    episode_id: uuid.UUID,
    edits: list[dict[str, object]],
    *,
    adopt_active_brand: bool = True,
    actor: str = "clip-studio",
    note: str | None = None,
) -> ShortEpisode:
    prepared, brand_snapshot, brand_version = _prepare_manifest(
        episode_id,
        edits,
        adopt_active_brand=adopt_active_brand,
    )
    child = register_short_episode_regeneration(
        episode_id,
        stage="render",
        actor=actor,
        note=note or "Manual Clip Studio render edit",
    )

    with session_scope() as session:
        row = session.get(ShortEpisode, child.id)
        if row is None:
            raise RuntimeError("studio child episode disappeared")
        prepared = prepared.model_copy(
            update={
                "short_episode_id": str(row.id),
                "output_key": f"short-episodes/{row.id}/renders/g1.mp4",
            }
        )
        if brand_snapshot is not None and brand_version is not None:
            row.brand_snapshot = brand_snapshot
            row.brand_key = prepared.brand.brand_key
            row.brand_version = brand_version
        row.render_manifest = RankedEpisodeRenderManifest.model_validate(
            prepared.model_dump(mode="json")
        ).model_dump(mode="json")
        row.status = "editorial_approved"
        row.stage = "studio_edit_ready"
        row.error = None
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=str(row.id),
                event_type="short_episode.studio_edit_applied",
                payload={
                    "short_episode_id": str(row.id),
                    "parent_episode_id": str(episode_id),
                    "edited_positions": sorted(int(edit["position"]) for edit in edits),
                    "adopt_active_brand": adopt_active_brand,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row
