"""Transactional Storyboard workspace history.

Workspaces are durable editing state. They may be incomplete and never authorize a
render, provider call, rights claim or publication by themselves.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.storyboard_schemas import (
    SaveStoryboardWorkspace,
    StoryboardWorkspace,
    UndoStoryboardWorkspace,
)
from katcha.editorial_models import (
    EditorialImage,
    EditorialNarration,
    EditorialProject,
    EditorialRevision,
    EditorialRun,
    EditorialStoryboardRevision,
)
from katcha.models import DomainEvent
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest


def _project(
    session: Session,
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
) -> EditorialProject:
    row = session.get(EditorialProject, project_id)
    if row is None or row.channel_profile_id != channel_id:
        raise EditorialNotFound("Editorial project not found in this channel")
    return row


def _validate_workspace(
    session: Session,
    project: EditorialProject,
    script_revision: int,
    workspace: StoryboardWorkspace,
) -> None:
    if project.revision != script_revision:
        raise EditorialConflict(
            "Script changed; reload before saving Storyboard edits"
        )
    revision = session.get(EditorialRevision, (project.id, script_revision))
    if revision is None:
        raise EditorialNotFound("Script revision not found in this project")
    draft = EditorialDraft.model_validate(revision.draft)
    expected_beats = [beat.id for beat in draft.script]
    draft_beats = {beat.id: beat for beat in draft.script}
    workspace_beats = [beat.beat_id for beat in workspace.beats]
    if workspace_beats != expected_beats:
        raise EditorialConflict(
            "Storyboard workspace must preserve the saved script beat order"
        )

    source_ids = {source.id for source in draft.sources}
    for beat in workspace.beats:
        if beat.quote_source_id and beat.quote_source_id not in source_ids:
            raise EditorialConflict(
                f"Storyboard beat {beat.beat_id} references an unknown research source"
            )

    media_ids = {
        use.candidate_id
        for beat in workspace.beats
        for use in beat.media
    }
    if media_ids:
        asset_run = session.get(EditorialRun, workspace.asset_run_id)
        if (
            asset_run is None
            or asset_run.project_id != project.id
            or asset_run.channel_profile_id != project.channel_profile_id
            or asset_run.input_revision != script_revision
            or asset_run.status != "completed"
            or dict(asset_run.options or {}).get("target") != "acquire_assets"
        ):
            raise EditorialConflict(
                "Storyboard footage must use a completed acquisition for this revision"
            )
        artifacts = dict(asset_run.artifacts or {})
        if revision.digest != artifacts.get("input_draft_digest"):
            raise EditorialConflict(
                "Storyboard acquisition does not match the frozen script"
            )
        acquired_ids = set(artifacts.get("acquired_assets") or {})
        if not media_ids <= acquired_ids:
            raise EditorialConflict(
                "Storyboard references footage outside its saved acquisition run"
            )
        selections = {
            item["id"]: item
            for item in artifacts.get("asset_selection", [])
            if isinstance(item, dict) and item.get("id")
        }
        for visual in workspace.beats:
            script_beat = draft_beats[visual.beat_id]
            for use in visual.media:
                selection = selections.get(use.candidate_id)
                if (
                    selection is None
                    or selection.get("beat_id") != script_beat.id
                    or set(selection.get("claim_ids", []))
                    != set(script_beat.claim_ids)
                ):
                    raise EditorialConflict(
                        "Storyboard footage must preserve its scouted beat "
                        "and claim references"
                    )

    for visual in workspace.beats:
        image_ids = (
            [visual.image_id]
            if visual.image_id is not None
            else list(visual.image_ids)
        )
        for image_id in image_ids:
            image = session.get(EditorialImage, image_id)
            if (
                image is None
                or image.project_id != project.id
                or image.channel_profile_id != project.channel_profile_id
                or image.revision != script_revision
                or image.beat_id != visual.beat_id
            ):
                raise EditorialConflict(
                    "Storyboard image was removed or belongs to another "
                    "script beat or revision"
                )

    beat_ids = set(expected_beats)
    for beat_id, narration_id in workspace.narration_ids.items():
        narration = session.get(EditorialNarration, narration_id)
        if (
            beat_id not in beat_ids
            or narration is None
            or narration.project_id != project.id
            or narration.channel_profile_id != project.channel_profile_id
            or narration.revision != script_revision
            or narration.beat_id != beat_id
        ):
            raise EditorialConflict(
                "Storyboard narration must belong to the matching saved beat"
            )


def _latest(
    session: Session,
    project_id: uuid.UUID,
    script_revision: int,
) -> EditorialStoryboardRevision | None:
    return session.scalar(
        select(EditorialStoryboardRevision)
        .where(
            EditorialStoryboardRevision.project_id == project_id,
            EditorialStoryboardRevision.script_revision == script_revision,
        )
        .order_by(EditorialStoryboardRevision.version.desc())
        .limit(1)
    )


def get_latest_storyboard(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    script_revision: int | None = None,
) -> EditorialStoryboardRevision | None:
    with session_scope() as session:
        project = _project(session, channel_id, project_id)
        revision = project.revision if script_revision is None else script_revision
        row = _latest(session, project_id, revision)
        if row is not None:
            session.expunge(row)
        return row


def list_storyboard_history(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    script_revision: int,
    offset: int = 0,
    limit: int = 20,
) -> list[EditorialStoryboardRevision]:
    with session_scope() as session:
        _project(session, channel_id, project_id)
        rows = list(
            session.scalars(
                select(EditorialStoryboardRevision)
                .where(
                    EditorialStoryboardRevision.project_id == project_id,
                    EditorialStoryboardRevision.script_revision == script_revision,
                )
                .order_by(EditorialStoryboardRevision.version.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def save_storyboard(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    request: SaveStoryboardWorkspace,
    *,
    actor: str,
    origin: Literal["operator", "ai_apply"] = "operator",
) -> EditorialStoryboardRevision:
    request_payload = request.model_dump(mode="json")
    request_digest = _digest(request_payload)
    request_id = uuid.uuid5(
        project_id,
        f"editorial-storyboard:{request.script_revision}:{request.idempotency_key}",
    )
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        project = _project(session, channel_id, project_id)
        session.execute(
            update(EditorialProject)
            .where(EditorialProject.id == project_id)
            .values(
                revision=EditorialProject.revision,
                updated_at=EditorialProject.updated_at,
            )
        )
        replay = session.scalar(
            select(EditorialStoryboardRevision).where(
                EditorialStoryboardRevision.request_id == request_id
            )
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise EditorialConflict(
                    "Storyboard save identity was already used for different edits"
                )
            session.expunge(replay)
            return replay

        _validate_workspace(
            session,
            project,
            request.script_revision,
            request.workspace,
        )
        current = _latest(session, project_id, request.script_revision)
        current_version = current.version if current is not None else 0
        if current_version != request.expected_version:
            raise EditorialConflict(
                "Storyboard changed; reload the current workspace before saving"
            )

        workspace_payload = request.workspace.model_dump(mode="json")
        row = EditorialStoryboardRevision(
            project_id=project_id,
            script_revision=request.script_revision,
            version=current_version + 1,
            channel_profile_id=channel_id,
            parent_version=current_version or None,
            request_id=request_id,
            request_digest=request_digest,
            digest=_digest(workspace_payload),
            workspace=workspace_payload,
            origin=origin,
            actor=actor,
        )
        session.add(row)
        session.add(
            DomainEvent(
                aggregate_type="editorial_storyboard",
                aggregate_id=(
                    f"{project_id}:{request.script_revision}:{row.version}"
                ),
                event_type="editorial.storyboard_saved",
                payload={
                    "channel_profile_id": str(channel_id),
                    "project_id": str(project_id),
                    "script_revision": request.script_revision,
                    "version": row.version,
                    "parent_version": row.parent_version,
                    "origin": origin,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def undo_storyboard(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    request: UndoStoryboardWorkspace,
    *,
    actor: str,
) -> EditorialStoryboardRevision:
    request_payload = request.model_dump(mode="json")
    request_digest = _digest(request_payload)
    request_id = uuid.uuid5(
        project_id,
        f"editorial-storyboard-undo:{request.script_revision}:{request.idempotency_key}",
    )
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        project = _project(session, channel_id, project_id)
        session.execute(
            update(EditorialProject)
            .where(EditorialProject.id == project_id)
            .values(
                revision=EditorialProject.revision,
                updated_at=EditorialProject.updated_at,
            )
        )
        replay = session.scalar(
            select(EditorialStoryboardRevision).where(
                EditorialStoryboardRevision.request_id == request_id
            )
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise EditorialConflict(
                    "Storyboard undo identity was already used for another version"
                )
            session.expunge(replay)
            return replay

        if project.revision != request.script_revision:
            raise EditorialConflict(
                "Script changed; reload before undoing Storyboard edits"
            )
        current = _latest(session, project_id, request.script_revision)
        if current is None or current.version != request.expected_version:
            raise EditorialConflict(
                "Storyboard changed; reload the current workspace before undoing"
            )
        target = (
            session.get(
                EditorialStoryboardRevision,
                (project_id, request.script_revision, current.parent_version),
            )
            if current.parent_version is not None
            else None
        )
        if current.parent_version is not None and target is None:
            raise EditorialConflict("Storyboard undo history is incomplete")
        if current.parent_version is None and (
            current.version != 1 or current.origin == "undo"
        ):
            raise EditorialConflict(
                "No earlier Storyboard edit is available to undo"
            )
        if target is None:
            revision = session.get(
                EditorialRevision,
                (project_id, request.script_revision),
            )
            if revision is None:
                raise EditorialNotFound("Script revision not found in this project")
            draft = EditorialDraft.model_validate(revision.draft)
            restored_workspace = StoryboardWorkspace(
                beats=[
                    {"beat_id": beat.id, "layout": "unassigned"}
                    for beat in draft.script
                ]
            )
            restored_digest = _digest(
                restored_workspace.model_dump(mode="json")
            )
            restored_parent = None
            restored_version = 0
        else:
            restored_workspace = StoryboardWorkspace.model_validate(
                target.workspace
            )
            restored_digest = target.digest
            restored_parent = target.parent_version
            restored_version = target.version

        row = EditorialStoryboardRevision(
            project_id=project_id,
            script_revision=request.script_revision,
            version=current.version + 1,
            channel_profile_id=channel_id,
            parent_version=restored_parent,
            request_id=request_id,
            request_digest=request_digest,
            digest=restored_digest,
            workspace=restored_workspace.model_dump(mode="json"),
            origin="undo",
            actor=actor,
        )
        session.add(row)
        session.add(
            DomainEvent(
                aggregate_type="editorial_storyboard",
                aggregate_id=(
                    f"{project_id}:{request.script_revision}:{row.version}"
                ),
                event_type="editorial.storyboard_undone",
                payload={
                    "channel_profile_id": str(channel_id),
                    "project_id": str(project_id),
                    "script_revision": request.script_revision,
                    "version": row.version,
                    "restored_version": restored_version,
                    "parent_version": row.parent_version,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row
