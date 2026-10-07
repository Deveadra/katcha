"""Compile frozen script and measured, currently cleared assets into inert visual data.

This boundary intentionally does not dispatch render work or approve publication.
"""

from __future__ import annotations

import math
import uuid
from contextlib import nullcontext

from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.editorial.assets import inspect_managed_candidate
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.visual_schemas import (
    EditorialRenderManifest,
    EditorialScene,
    RenderCaption,
    RenderImage,
    RenderMedia,
    RenderNarration,
    StoryboardPlan,
)
from katcha.editorial_models import EditorialProject, EditorialRevision, EditorialRun
from katcha.models import Clip
from katcha.rendering.manifest import ShortBrandSpec
from katcha.services.channel_brands import brand_for_channel
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest


def compile_visuals(
    *,
    project_id: str,
    revision: int,
    draft: EditorialDraft,
    plan: StoryboardPlan,
    media: list[RenderMedia],
    narration: list[RenderNarration] | None = None,
    images: list[RenderImage] | None = None,
    brand: ShortBrandSpec | None = None,
) -> EditorialRenderManifest:
    if [beat.beat_id for beat in plan.beats] != [beat.id for beat in draft.script]:
        raise EditorialConflict("Storyboard must cover each script beat once, in script order")
    sources = {source.id: source for source in draft.sources}
    claims = {claim.id: claim for claim in draft.claims}
    cursor = 0
    timeline = []
    audio_by_beat = {item.beat_id: item for item in narration or []}
    for beat, visual in zip(draft.script, plan.beats, strict=True):
        if beat.uncertainty_disclosure and len(beat.uncertainty_disclosure) > 180:
            raise EditorialConflict(
                "Shorten the on-screen uncertainty disclosure to 180 characters"
            )
        frames = max(1, math.ceil(beat.planned_duration_seconds * 30))
        if plan.presentation_mode == "narrated":
            audio = audio_by_beat.get(beat.id)
            if audio is None or audio.text_digest != _digest({"text": beat.narration}):
                raise EditorialConflict("Narration does not match this saved script beat")
            frames = (audio.sample_frames * 30 + audio.sample_rate - 1) // audio.sample_rate
        words = beat.narration.split()
        if plan.presentation_mode == "captioned_silent" and len(words) / (frames / 30) > 3.5:
            raise EditorialConflict("Caption-only script is too fast to read; adjust beat duration")
        # Small, bounded chunks avoid filling the entire frame with a paragraph.
        chunks = [" ".join(words[index : index + 12]) for index in range(0, len(words), 12)]
        captions = []
        for index, chunk in enumerate(chunks):
            start = frames * index // len(chunks)
            end = frames * (index + 1) // len(chunks)
            captions.append(
                RenderCaption(start_frame=start, duration_frames=end - start, text=chunk)
            )
        quote = None
        credit = None
        if visual.quote_source_id:
            linked_sources = {
                source for claim_id in beat.claim_ids for source in claims[claim_id].source_ids
            }
            if visual.quote_source_id not in linked_sources:
                raise EditorialConflict("Quote card must cite evidence belonging to this beat")
            source = sources[visual.quote_source_id]
            quote = source.excerpt if len(source.excerpt) <= 280 else source.excerpt[:277] + "…"
            credit = source.title if len(source.title) <= 200 else source.title[:197] + "…"
        timeline.append(
            EditorialScene(
                **visual.model_dump(mode="json"),
                start_frame=cursor,
                duration_frames=frames,
                captions=captions,
                uncertainty_disclosure=beat.uncertainty_disclosure,
                quote_text=quote,
                source_credit=credit,
            )
        )
        cursor += frames
    value = dict(
        project_id=project_id,
        revision=revision,
        draft_digest=_digest(draft.model_dump(mode="json")),
        presentation_mode=plan.presentation_mode,
        media=media,
        timeline=timeline,
        output_duration_seconds=cursor / 30,
    )
    if plan.presentation_mode == "narrated":
        value.update(
            version="editorial-render-v2",
            narration=[audio_by_beat[beat.id].model_dump(mode="json") for beat in draft.script],
        )
    if images:
        value.update(
            version="editorial-render-v3", images=[item.model_dump(mode="json") for item in images]
        )
    if any(
        beat.layout == "image_comparison" or (beat.layout == "image" and beat.overlays)
        for beat in plan.beats
    ):
        value["version"] = "editorial-render-v4"
    if any(
        beat.caption_position != "bottom"
        or beat.caption_scale != 1
        or beat.caption_background
        or beat.transition != "cut"
        or any(use.crop is not None for use in beat.media)
        for beat in plan.beats
    ):
        value["version"] = "editorial-render-v5"
    if brand is not None:
        value.update(
            version="editorial-render-v6",
            brand=brand.model_dump(mode="json"),
        )
    digest = _digest(
        {
            **value,
            "media": [item.model_dump(mode="json") for item in media],
            "timeline": [item.model_dump(mode="json") for item in timeline],
        }
    )
    return EditorialRenderManifest(
        **value, output_key=f"editorial/{project_id}/{revision}/{digest}.mp4"
    )


def compile_project_visuals(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    expected_revision: int,
    asset_run_id: uuid.UUID | None,
    plan: StoryboardPlan,
    *,
    session: Session | None = None,
) -> EditorialRenderManifest:
    """Server-side resolution: storage keys and rights flags never come from a model/client."""
    with session_scope() if session is None else nullcontext(session) as session:
        ensure_active_profile(session, channel_id)
        project = session.get(EditorialProject, project_id)
        if project is None or project.channel_profile_id != channel_id:
            raise EditorialNotFound("Editorial project not found in this channel")
        if project.revision != expected_revision:
            raise EditorialConflict(
                "Script changed; rebuild the storyboard for its current revision"
            )
        revision = session.get(EditorialRevision, (project_id, expected_revision))
        brand_contract, brand_version = brand_for_channel(session, channel_id)
        brand = ShortBrandSpec.model_validate(dict(brand_contract.visual))
        if brand.brand_key != brand_contract.brand_key or brand.version != brand_version:
            raise EditorialConflict(
                "Active channel brand identity is inconsistent; reactivate branding"
            )
        run = session.get(EditorialRun, asset_run_id) if asset_run_id else None
        uses_video = any(beat.media for beat in plan.beats)
        if (uses_video or asset_run_id) and (
            run is None
            or run.project_id != project_id
            or run.channel_profile_id != channel_id
            or run.input_revision != expected_revision
            or run.status != "completed"
            or run.options.get("target") != "acquire_assets"
        ):
            raise EditorialConflict("Select completed asset acquisition for this script revision")
        if revision is None or (run and revision.digest != run.artifacts.get("input_draft_digest")):
            raise EditorialConflict("Acquired assets do not match the frozen script")
        draft = EditorialDraft.model_validate(revision.draft)
        from katcha.editorial.narration import resolve_narration

        narration = resolve_narration(
            session, channel_id, project_id, expected_revision, draft, plan
        )
        from katcha.editorial.images import resolve_images

        images = resolve_images(session, channel_id, project_id, expected_revision, plan)
        artifacts = run.artifacts if run else {}
        receipts = dict(artifacts.get("acquired_assets") or {})
        used = {item.candidate_id for beat in plan.beats for item in beat.media}
        if not used <= receipts.keys():
            raise EditorialConflict("Storyboard references an asset outside this acquisition")
        selections = {item["id"]: item for item in artifacts.get("asset_selection", [])}
        beats = {beat.id: beat for beat in draft.script}
        for visual in plan.beats:
            beat = beats.get(visual.beat_id)
            for use in visual.media:
                selection = selections.get(use.candidate_id)
                if (
                    beat is None
                    or selection is None
                    or selection.get("beat_id") != beat.id
                    or set(selection.get("claim_ids", [])) != set(beat.claim_ids)
                ):
                    raise EditorialConflict(
                        "Visual media must preserve its scouted beat and claim references"
                    )
        resolved = []
        for identity in sorted(used):
            receipt = receipts[identity]
            current = inspect_managed_candidate(receipt["source_url"], channel_id, session=session)
            if not current["production_eligible"]:
                raise EditorialConflict(
                    "Storyboard media needs current rights, audio and originality clearance"
                )
            if current["clip_id"] != receipt["clip_id"] or current["sha256"] != receipt["sha256"]:
                raise EditorialConflict(
                    "Acquired media changed; acquire and review the current asset"
                )
            clip = session.get(Clip, uuid.UUID(receipt["clip_id"]))
            if (
                clip is None
                or clip.storage_key != receipt["storage_key"]
                or float(clip.duration_seconds or 0) != receipt["duration_seconds"]
            ):
                raise EditorialConflict("Measured source identity changed")
            resolved.append(
                RenderMedia(
                    candidate_id=identity,
                    clip_id=str(clip.id),
                    storage_key=clip.storage_key,
                    sha256=clip.sha256,
                    width=clip.width,
                    height=clip.height,
                    duration_seconds=float(clip.duration_seconds),
                    rights_assessment_id=current["rights_assessment_id"],
                )
            )
    return compile_visuals(
        project_id=str(project_id),
        revision=expected_revision,
        draft=draft,
        plan=plan,
        media=resolved,
        narration=narration,
        images=images,
        brand=brand,
    )
