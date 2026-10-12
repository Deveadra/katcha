"""Approved measured choices into existing Storyboard and render contracts.

This adapter revalidates Agent A's saved output; it never matches or ranks footage.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from contextlib import nullcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from katcha.acquisition_models import RightsAssessment
from katcha.db import session_scope
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.shot_handoff_schemas import ApplyApprovedShot
from katcha.editorial.source_intelligence.contracts import digest
from katcha.editorial.source_intelligence.requirements import AssetManifestV1
from katcha.editorial.source_intelligence.shot_contracts import (
    RequirementBindingV1,
    ShotProposalPackageV1,
)
from katcha.editorial.storyboard_schemas import (
    SaveStoryboardWorkspace,
    StoryboardWorkspace,
    StoryboardWorkspaceBeat,
)
from katcha.editorial.visual_schemas import RenderMedia, VisualBeat, VisualMediaUse
from katcha.editorial_models import (
    EditorialProject,
    EditorialRevision,
    EditorialRun,
    EditorialStoryboardRevision,
    ProjectMediaItem,
)
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip
from katcha.services.editorial_media_preparation import _seed
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest
from katcha.services.editorial_shot_matching import POLICY, _links, _snapshot
from katcha.shot_matching_models import OperatorShotChoice, RequirementShotPackage

SHOT_PREFIX = "approved_shot_"


def choice_media_id(choice_id: uuid.UUID) -> str:
    return SHOT_PREFIX + choice_id.hex


def is_shot_choice(identity: str) -> bool:
    return identity.startswith(SHOT_PREFIX)


def resolve_approved_shots(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    revision: int,
    identities: set[str],
    *,
    session: Session | None = None,
    store: ObjectStore | None = None,
) -> dict[str, tuple[RequirementBindingV1, RenderMedia]]:
    """Resolve current choices and canonical media without any acquisition/provider calls."""
    if len(identities) > 30:
        raise EditorialConflict("Storyboard exceeds the thirty-shot media limit")
    resolved = {}
    packages: dict[str, Any] = {}
    with session_scope() if session is None else nullcontext(session) as active:
        choices = []
        for identity in sorted(identities):
            if not is_shot_choice(identity):
                continue
            try:
                choice_id = uuid.UUID(hex=identity[len(SHOT_PREFIX) :])
            except ValueError as exc:
                raise EditorialConflict("Invalid approved shot identity") from exc
            if choice_media_id(choice_id) != identity:
                raise EditorialConflict("Invalid approved shot identity")
            choice = active.get(OperatorShotChoice, choice_id)
            if (
                choice is None
                or choice.channel_profile_id != channel_id
                or choice.project_id != project_id
            ):
                raise EditorialNotFound("Approved shot choice not found in this project/channel")
            choices.append((identity, choice))
        if len({row.payload["package_id"] for _, row in choices}) > 8:
            raise EditorialConflict("Storyboard exceeds the eight-package verification limit")
        verified_items: set[uuid.UUID] = set()
        for _, choice in choices:
            package_row = active.get(
                RequirementShotPackage, uuid.UUID(choice.payload["package_id"])
            )
            if (
                package_row is None
                or package_row.channel_profile_id != channel_id
                or package_row.project_id != project_id
                or package_row.input_revision != revision
            ):
                raise EditorialConflict("Approved shot evidence is stale or outside this project")
            verified_items.update(
                ShotProposalPackageV1.model_validate(package_row.payload).item_ids
            )
        if len(verified_items) > 8:
            raise EditorialConflict("Storyboard exceeds the eight-footage verification limit")
        if verified_items:
            # Scope and verify unique canonical bytes once across the entire handoff/render.
            _snapshot(channel_id, project_id, tuple(sorted(verified_items)), store=store)
        for identity, row in choices:
            package_id = row.payload["package_id"]
            if package_id not in packages:
                # The public package reader serializes the project. Inside Storyboard/review
                # write transactions, consume its immutable contract and reuse the same
                # read-only evidence snapshot to avoid taking a second project write lock.
                package_row = active.get(RequirementShotPackage, uuid.UUID(package_id))
                if (
                    package_row is None
                    or package_row.channel_profile_id != channel_id
                    or package_row.project_id != project_id
                    or package_row.input_revision != revision
                ):
                    raise EditorialConflict(
                        "Approved shot evidence is stale or outside this project"
                    )
                payload = ShotProposalPackageV1.model_validate(package_row.payload)
                project = active.get(EditorialProject, project_id)
                script = active.get(EditorialRevision, (project_id, revision))
                preparation = active.get(EditorialRun, payload.preparation_run_id)
                if (
                    project is None
                    or project.channel_profile_id != channel_id
                    or project.revision != revision
                    or script is None
                    or preparation is None
                    or preparation.channel_profile_id != channel_id
                    or preparation.project_id != project_id
                    or preparation.input_revision != revision
                    or preparation.options.get("target") != "prepare_media"
                ):
                    raise EditorialConflict(
                        "Script or preparation changed; revalidate approved shots"
                    )
                manifest = AssetManifestV1.model_validate(preparation.artifacts["manifest"])
                seed, _ = _seed(project, script)
                if (
                    manifest.scope != payload.scope
                    or manifest.scope.channel_id != channel_id
                    or manifest.scope.project_id != project_id
                    or manifest.scope.revision_digest != seed.content_sha256
                ):
                    raise EditorialConflict("Approved shot script evidence is stale")
                _, _, stamp = _snapshot(
                    channel_id, project_id, payload.item_ids, store=store, verify_bytes=False
                )
                links = _links(payload.preparation_run_id)
                dependency = digest(
                    {
                        "snapshot": stamp,
                        "manifest": manifest.model_dump(mode="json"),
                        "links": {key: sorted(value) for key, value in links.items()},
                        "policy": POLICY,
                    }
                )
                if dependency != package_row.dependency_digest:
                    raise EditorialConflict(
                        "Approved shot evidence is stale; revalidate source and permission"
                    )
                packages[package_id] = payload
            latest_choice = active.scalar(
                select(OperatorShotChoice)
                .where(
                    OperatorShotChoice.project_id == project_id,
                    OperatorShotChoice.channel_profile_id == channel_id,
                    OperatorShotChoice.requirement_id == row.requirement_id,
                )
                .order_by(OperatorShotChoice.version.desc())
                .limit(1)
            )
            if (
                latest_choice is None
                or latest_choice.id != row.id
                or row.payload["state"] != "operator_selected"
            ):
                raise EditorialConflict(
                    "Approved shot choice was replaced; select its current version"
                )
            current = row.payload
            binding = RequirementBindingV1.model_validate(current["binding"])
            if not binding.production_eligible:
                raise EditorialConflict(
                    "This shot needs current production permission before Storyboard use"
                )
            item = active.get(ProjectMediaItem, binding.media_item_id)
            assessment = active.get(RightsAssessment, binding.rights_assessment_id)
            if item is not None:
                active.refresh(item)
            if assessment is not None:
                active.refresh(assessment)
            latest = (
                active.scalar(
                    select(RightsAssessment)
                    .where(
                        RightsAssessment.discovery_candidate_id == assessment.discovery_candidate_id
                    )
                    .order_by(RightsAssessment.version.desc())
                    .limit(1)
                )
                if assessment
                else None
            )
            if (
                item is None
                or item.channel_profile_id != channel_id
                or item.project_id != project_id
                or item.rights_status != "production_cleared"
                or item.rights_assessment_id != binding.rights_assessment_id
                or item.rights_assessment_version != binding.rights_assessment_version
                or assessment is None
                or latest is None
                or latest.id != assessment.id
                or not latest.production_eligible
            ):
                raise EditorialConflict("Shot production permission changed; revalidate before use")
            clip = active.get(Clip, binding.clip_id)
            if clip is not None:
                active.refresh(clip)
            if (
                clip is None
                or clip.sha256 != binding.clip_sha256
                or not clip.storage_key
                or clip.width is None
                or clip.height is None
            ):
                raise EditorialConflict("Approved shot canonical media changed")
            resolved[identity] = (
                binding,
                RenderMedia(
                    candidate_id=identity,
                    clip_id=str(clip.id),
                    storage_key=clip.storage_key,
                    sha256=clip.sha256,
                    width=clip.width,
                    height=clip.height,
                    duration_seconds=float(clip.duration_seconds or 0),
                    rights_assessment_id=str(binding.rights_assessment_id),
                ),
            )
    return resolved


def validate_approved_intervals(
    beats: Iterable[VisualBeat | StoryboardWorkspaceBeat],
    resolved: dict[str, tuple[RequirementBindingV1, RenderMedia]],
) -> None:
    """Edits may stay within a measured shot; playback can never spill into another shot."""
    for beat in beats:
        for use in beat.media:
            if not is_shot_choice(use.candidate_id):
                continue
            binding = resolved[use.candidate_id][0]
            end = use.start_seconds
            if hasattr(beat, "duration_frames") and not use.freeze:
                end += beat.duration_frames / 30 * use.playback_rate
            if (
                use.start_seconds < binding.source_in_seconds
                or use.start_seconds >= binding.source_out_seconds
                or end > binding.source_out_seconds + 1e-6
            ):
                raise EditorialConflict(
                    "Playback exceeds the approved measured interval. Shorten the beat, "
                    "slow playback within its bounds, or explicitly hold a measured frame."
                )


def apply_approved_shot(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    request: ApplyApprovedShot,
    *,
    actor: str,
    store: ObjectStore | None = None,
) -> EditorialStoryboardRevision:
    from katcha.services.editorial_storyboards import get_latest_storyboard, save_storyboard

    identity = choice_media_id(request.choice_id)
    resolved = resolve_approved_shots(
        channel_id, project_id, request.expected_revision, {identity}, store=store
    )
    binding = resolved[identity][0]
    current = get_latest_storyboard(channel_id, project_id)
    # Reuse the existing save replay identity, including an uncertain response.
    # A replay returns its historical workspace; all use still revalidates at compile/render.
    with session_scope() as session:
        replay_id = uuid.uuid5(
            project_id,
            f"editorial-storyboard:{request.expected_revision}:{request.idempotency_key}",
        )
        replay = session.query(EditorialStoryboardRevision).filter_by(request_id=replay_id).first()
        if replay is not None:
            if replay.channel_profile_id != channel_id or replay.request_digest != _digest(
                {"shot_handoff": request.model_dump(mode="json")}
            ):
                raise EditorialConflict("Handoff identity was reused for different input")
            session.expunge(replay)
            return replay
        revision = session.get(EditorialRevision, (project_id, request.expected_revision))
        if revision is None:
            raise EditorialNotFound("Saved script revision not found")
        draft = EditorialDraft.model_validate(revision.draft)
    if not draft.script:
        raise EditorialConflict("Save script beats before binding approved shots to Storyboard")
    if current and current.version != request.expected_storyboard_version:
        raise EditorialConflict("Storyboard changed; reload before applying an approved shot")
    if not current and request.expected_storyboard_version != 0:
        raise EditorialConflict("Storyboard changed; reload its current version")
    workspace = (
        StoryboardWorkspace.model_validate(current.workspace)
        if current
        else StoryboardWorkspace(
            beats=[StoryboardWorkspaceBeat(beat_id=beat.id) for beat in draft.script]
        )
    )
    original = next((beat for beat in workspace.beats if beat.beat_id == request.beat_id), None)
    if original is None:
        raise EditorialConflict("Select a beat from the current saved script")
    if original.layout != "unassigned" and not request.replace_existing:
        raise EditorialConflict(
            "This beat has a manual visual. Confirm replacement or choose an unassigned beat"
        )
    start = (
        binding.keyframe_tick * binding.timebase_numerator / binding.timebase_denominator
        if request.freeze
        else binding.source_in_seconds
    )
    updated = StoryboardWorkspaceBeat(
        beat_id=original.beat_id,
        layout="single",
        media=[VisualMediaUse(candidate_id=identity, start_seconds=start, freeze=request.freeze)],
        caption_position=original.caption_position,
        caption_scale=original.caption_scale,
        caption_background=original.caption_background,
        transition=original.transition,
        transition_frames=original.transition_frames,
    )
    beats = [updated if beat.beat_id == original.beat_id else beat for beat in workspace.beats]
    legacy_media = any(not is_shot_choice(use.candidate_id) for beat in beats for use in beat.media)
    new_workspace = StoryboardWorkspace(
        presentation_mode=workspace.presentation_mode,
        beats=beats,
        narration_ids=workspace.narration_ids,
        asset_run_id=workspace.asset_run_id if legacy_media else None,
    )
    return save_storyboard(
        channel_id,
        project_id,
        SaveStoryboardWorkspace(
            script_revision=request.expected_revision,
            expected_version=request.expected_storyboard_version,
            workspace=new_workspace,
            idempotency_key=request.idempotency_key,
        ),
        actor=actor,
        operation_receipt={"shot_handoff": request.model_dump(mode="json")},
    )
