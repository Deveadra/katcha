from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from katcha.api.control_auth import control_actor, require_control_channel, require_control_scope
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.editorial.project_schemas import (
    CreateEditorialProject,
    EditorialProjectResponse,
    EditorialRevisionResponse,
    SaveEditorialDraft,
)
from katcha.editorial.storyboard_schemas import (
    SaveStoryboardWorkspace,
    StoryboardWorkspaceResponse,
    UndoStoryboardWorkspace,
)
from katcha.editorial.visual_compiler import compile_project_visuals
from katcha.editorial.visual_schemas import EditorialRenderManifest, StoryboardPreflightRequest
from katcha.editorial_models import (
    EditorialProject,
    EditorialRevision,
    EditorialStoryboardRevision,
)
from katcha.services.editorial_projects import (
    EditorialConflict,
    EditorialNotFound,
    create_project,
    get_project,
    save_draft,
)
from katcha.services.editorial_storyboards import (
    get_latest_storyboard,
    list_storyboard_history,
    save_storyboard,
    undo_storyboard,
)

router = APIRouter(
    prefix="/v1/channels/{channel_profile_id}/editorial-projects", tags=["editorial-projects"]
)


@router.post("/source-uploads", status_code=201)
async def source_media_upload(
    channel_profile_id: uuid.UUID,
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    title: str | None = Query(default=None, max_length=300),
    idempotency_key: str = Query(min_length=1, max_length=120),
    permitted_use: bool = Query(False),
):
    from katcha.editorial.source_uploads import (
        MAX_SOURCE_UPLOAD_BYTES,
        import_source_media,
    )

    _authorize(request, channel_profile_id, write=True)
    content_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_SOURCE_UPLOAD_BYTES:
                raise HTTPException(413, "Upload a source video no larger than 512 MiB")
        except ValueError:
            pass

    settings = get_settings()
    suffix = Path(filename).suffix[:16]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="editorial-source-",
            suffix=suffix,
            dir=settings.work_dir,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_SOURCE_UPLOAD_BYTES:
                    raise HTTPException(413, "Upload a source video no larger than 512 MiB")
                handle.write(chunk)
        return await run_in_threadpool(
            import_source_media,
            channel_profile_id,
            temporary,
            filename=filename,
            content_type=content_type,
            title=title,
            permitted_use=permitted_use,
            idempotency_key=idempotency_key,
            actor=control_actor(request),
        )
    except ValueError as exc:
        raise _error(exc) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@router.get("/{project_id}/images")
def images_list(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    revision: int = Query(ge=1),
):
    from katcha.editorial.images import list_images

    _authorize(request, channel_profile_id)
    try:
        return list_images(channel_profile_id, project_id, revision)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{project_id}/images", status_code=201)
async def image_upload(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    revision: int = Query(ge=1),
    beat_id: str = Query(min_length=1, max_length=120),
    idempotency_key: str = Query(min_length=1, max_length=120),
    title: str = Query(min_length=1, max_length=200),
    source_reference: str = Query(min_length=1, max_length=2000),
    use_note: str = Query(min_length=1, max_length=2000),
    illustration: bool = Query(False),
    permitted_use: bool = Query(False),
):
    from katcha.editorial.images import MAX_IMAGE_BYTES, ImageUpload, import_image

    _authorize(request, channel_profile_id, write=True)
    try:
        await run_in_threadpool(get_project, channel_profile_id, project_id)
        metadata = ImageUpload(
            revision=revision,
            beat_id=beat_id,
            idempotency_key=idempotency_key,
            title=title,
            source_reference=source_reference,
            use_note=use_note,
            illustration=illustration,
            permitted_use=permitted_use,
        )
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                raise HTTPException(413, "Upload an image no larger than 16 MiB")
            data.extend(chunk)
        return await run_in_threadpool(
            import_image,
            channel_profile_id,
            project_id,
            request=metadata,
            data=bytes(data),
            actor=control_actor(request),
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{project_id}/images/{image_id}/revoke")
def image_revoke(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, image_id: uuid.UUID, request: Request
):
    from katcha.editorial.images import revoke_image

    _authorize(request, channel_profile_id, write=True)
    try:
        return revoke_image(channel_profile_id, project_id, image_id, actor=control_actor(request))
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{project_id}/narration")
def narration_list(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    revision: int = Query(ge=1),
):
    from katcha.editorial.narration import list_narration

    _authorize(request, channel_profile_id)
    try:
        return list_narration(channel_profile_id, project_id, revision)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{project_id}/narration", status_code=201)
async def narration_upload(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    revision: int = Query(ge=1),
    beat_id: str = Query(min_length=1, max_length=120),
    idempotency_key: str = Query(min_length=1, max_length=120),
    permitted_use: bool = Query(False),
):
    from katcha.editorial.narration import MAX_AUDIO_BYTES, import_narration

    _authorize(request, channel_profile_id, write=True)
    try:
        await run_in_threadpool(get_project, channel_profile_id, project_id)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_AUDIO_BYTES:
                raise HTTPException(413, "Upload a WAV recording no larger than 32 MiB")
            data.extend(chunk)
        return await run_in_threadpool(
            import_narration,
            channel_profile_id,
            project_id,
            revision=revision,
            beat_id=beat_id,
            idempotency_key=idempotency_key,
            audio=bytes(data),
            actor=control_actor(request),
            permitted_use=permitted_use,
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{project_id}/narration/{narration_id}/revoke")
def narration_revoke(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    narration_id: uuid.UUID,
    request: Request,
):
    from katcha.editorial.narration import revoke_narration

    _authorize(request, channel_profile_id, write=True)
    try:
        return revoke_narration(
            channel_profile_id, project_id, narration_id, actor=control_actor(request)
        )
    except ValueError as exc:
        raise _error(exc) from exc


def _authorize(request: Request, channel_id: uuid.UUID, *, write: bool = False) -> None:
    require_control_channel(request, channel_id)
    require_control_scope(request, "production:create" if write else "ai:read")


def _project(row: EditorialProject) -> EditorialProjectResponse:
    return EditorialProjectResponse(
        id=str(row.id),
        channel_profile_id=str(row.channel_profile_id),
        brief=row.brief,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision(row: EditorialRevision) -> EditorialRevisionResponse:
    return EditorialRevisionResponse(
        revision=row.revision,
        digest=row.digest,
        draft=row.draft,
        created_at=row.created_at,
        actor=row.actor,
    )


def _storyboard(
    row: EditorialStoryboardRevision,
) -> StoryboardWorkspaceResponse:
    return StoryboardWorkspaceResponse(
        script_revision=row.script_revision,
        version=row.version,
        parent_version=row.parent_version,
        digest=row.digest,
        workspace=row.workspace,
        origin=row.origin,
        actor=row.actor,
        created_at=row.created_at,
    )


def _error(exc: ValueError) -> HTTPException:
    if isinstance(exc, EditorialNotFound):
        return HTTPException(404, str(exc))
    return HTTPException(409 if isinstance(exc, EditorialConflict) else 422, str(exc))


@router.post("", response_model=EditorialProjectResponse, status_code=201)
def create(
    channel_profile_id: uuid.UUID, body: CreateEditorialProject, request: Request
) -> EditorialProjectResponse:
    _authorize(request, channel_profile_id, write=True)
    try:
        return _project(create_project(channel_profile_id, body, actor=control_actor(request)))
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("", response_model=list[EditorialProjectResponse])
def list_projects(
    channel_profile_id: uuid.UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(20, ge=1, le=100),
) -> list[EditorialProjectResponse]:
    _authorize(request, channel_profile_id)
    with session_scope() as session:
        rows = session.scalars(
            select(EditorialProject)
            .where(EditorialProject.channel_profile_id == channel_profile_id)
            .order_by(EditorialProject.created_at.desc(), EditorialProject.id)
            .offset(offset)
            .limit(limit)
        )
        return [_project(row) for row in rows]


@router.get("/{project_id}", response_model=EditorialProjectResponse)
def detail(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, request: Request
) -> EditorialProjectResponse:
    _authorize(request, channel_profile_id)
    try:
        return _project(get_project(channel_profile_id, project_id))
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{project_id}/revisions", response_model=EditorialRevisionResponse, status_code=201)
def save(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, body: SaveEditorialDraft, request: Request
) -> EditorialRevisionResponse:
    _authorize(request, channel_profile_id, write=True)
    try:
        return _revision(
            save_draft(channel_profile_id, project_id, body, actor=control_actor(request))
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{project_id}/revisions", response_model=list[EditorialRevisionResponse])
def history(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(10, ge=1, le=20),
) -> list[EditorialRevisionResponse]:
    _authorize(request, channel_profile_id)
    try:
        get_project(channel_profile_id, project_id)
    except ValueError as exc:
        raise _error(exc) from exc
    with session_scope() as session:
        rows = session.scalars(
            select(EditorialRevision)
            .where(EditorialRevision.project_id == project_id)
            .order_by(EditorialRevision.revision.desc())
            .offset(offset)
            .limit(limit)
        )
        return [_revision(row) for row in rows]


@router.get("/{project_id}/revisions/{revision}", response_model=EditorialRevisionResponse)
def revision_detail(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    revision: int,
    request: Request,
) -> EditorialRevisionResponse:
    _authorize(request, channel_profile_id)
    try:
        get_project(channel_profile_id, project_id)
    except ValueError as exc:
        raise _error(exc) from exc
    with session_scope() as session:
        row = session.get(EditorialRevision, (project_id, revision))
        if row is None:
            raise HTTPException(404, "Script revision not found in this project")
        return _revision(row)


@router.get(
    "/{project_id}/storyboard",
    response_model=StoryboardWorkspaceResponse | None,
)
def storyboard_workspace(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    script_revision: int | None = Query(default=None, ge=1),
) -> StoryboardWorkspaceResponse | None:
    _authorize(request, channel_profile_id)
    try:
        row = get_latest_storyboard(
            channel_profile_id,
            project_id,
            script_revision=script_revision,
        )
        return _storyboard(row) if row is not None else None
    except ValueError as exc:
        raise _error(exc) from exc


@router.get(
    "/{project_id}/storyboard/history",
    response_model=list[StoryboardWorkspaceResponse],
)
def storyboard_history(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    request: Request,
    script_revision: int = Query(ge=1),
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(20, ge=1, le=100),
) -> list[StoryboardWorkspaceResponse]:
    _authorize(request, channel_profile_id)
    try:
        return [
            _storyboard(row)
            for row in list_storyboard_history(
                channel_profile_id,
                project_id,
                script_revision=script_revision,
                offset=offset,
                limit=limit,
            )
        ]
    except ValueError as exc:
        raise _error(exc) from exc


@router.post(
    "/{project_id}/storyboard",
    response_model=StoryboardWorkspaceResponse,
    status_code=201,
)
def save_storyboard_workspace(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: SaveStoryboardWorkspace,
    request: Request,
) -> StoryboardWorkspaceResponse:
    _authorize(request, channel_profile_id, write=True)
    try:
        return _storyboard(
            save_storyboard(
                channel_profile_id,
                project_id,
                body,
                actor=control_actor(request),
            )
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post(
    "/{project_id}/storyboard/undo",
    response_model=StoryboardWorkspaceResponse,
    status_code=201,
)
def undo_storyboard_workspace(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: UndoStoryboardWorkspace,
    request: Request,
) -> StoryboardWorkspaceResponse:
    _authorize(request, channel_profile_id, write=True)
    try:
        return _storyboard(
            undo_storyboard(
                channel_profile_id,
                project_id,
                body,
                actor=control_actor(request),
            )
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/{project_id}/storyboard/preflight")
def preflight_storyboard(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    body: StoryboardPreflightRequest,
    request: Request,
) -> EditorialRenderManifest:
    """Validate a storyboard without rendering, spending or approving publication."""
    _authorize(request, channel_profile_id, write=True)
    try:
        return compile_project_visuals(
            channel_profile_id, project_id, body.expected_revision, body.asset_run_id, body.plan
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get(
    "/{project_id}/runs/{run_id}/assets/{candidate_id}/source-monitor"
)
def storyboard_source_monitor(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: str,
    request: Request,
):
    from katcha.editorial.source_monitor import source_monitor

    _authorize(request, channel_profile_id)
    try:
        result = source_monitor(channel_profile_id, project_id, run_id, candidate_id)
        hidden = {"contact_sheet_key", "storage_key"}
        return {key: value for key, value in result.items() if key not in hidden}
    except ValueError as exc:
        raise _error(exc) from exc


@router.get(
    "/{project_id}/runs/{run_id}/assets/{candidate_id}/contact-sheet"
)
def storyboard_source_monitor_image(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: str,
    request: Request,
):
    from katcha.api.studio import _stream_object
    from katcha.editorial.source_monitor import source_monitor

    _authorize(request, channel_profile_id)
    try:
        result = source_monitor(channel_profile_id, project_id, run_id, candidate_id)
        return _stream_object(
            request,
            str(result["contact_sheet_key"]),
            f"editorial-{project_id}-{candidate_id}-contact-sheet.jpg",
        )
    except ValueError as exc:
        raise _error(exc) from exc


def _source_media_scope(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: str,
) -> dict[str, str]:
    return {
        "kind": "editorial_source_media",
        "channel_profile_id": str(channel_profile_id),
        "project_id": str(project_id),
        "run_id": str(run_id),
        "candidate_id": candidate_id,
    }


@router.post(
    "/{project_id}/runs/{run_id}/assets/{candidate_id}/source-media-session"
)
def storyboard_source_media_session(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: str,
    request: Request,
    response: Response,
):
    from katcha.editorial.source_monitor import source_monitor
    from katcha.security.media_tickets import (
        SOURCE_MEDIA_COOKIE,
        SOURCE_MEDIA_TICKET_TTL_SECONDS,
        issue_media_ticket,
    )
    from katcha.security.secrets import SecretConfigurationError

    _authorize(request, channel_profile_id)
    try:
        source_monitor(channel_profile_id, project_id, run_id, candidate_id)
        scope = _source_media_scope(
            channel_profile_id,
            project_id,
            run_id,
            candidate_id,
        )
        token = issue_media_ticket(scope)
        encoded_candidate = quote(candidate_id, safe="")
        media_path = (
            f"/v1/channels/{channel_profile_id}/editorial-projects/{project_id}"
            f"/runs/{run_id}/assets/{encoded_candidate}/source-media"
        )
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            SOURCE_MEDIA_COOKIE,
            token,
            max_age=SOURCE_MEDIA_TICKET_TTL_SECONDS,
            httponly=True,
            secure=get_settings().env == "production",
            samesite="strict",
            path=media_path,
        )
        return {
            "media_url": media_path,
            "expires_in_seconds": SOURCE_MEDIA_TICKET_TTL_SECONDS,
        }
    except SecretConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise _error(exc) from exc


@router.get(
    "/{project_id}/runs/{run_id}/assets/{candidate_id}/source-media"
)
def storyboard_source_media(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: str,
    request: Request,
):
    from katcha.api.studio import _stream_object
    from katcha.editorial.source_monitor import source_monitor
    from katcha.security.media_tickets import (
        SOURCE_MEDIA_COOKIE,
        verify_media_ticket,
    )
    from katcha.security.secrets import SecretConfigurationError

    scope = _source_media_scope(
        channel_profile_id,
        project_id,
        run_id,
        candidate_id,
    )
    try:
        verify_media_ticket(
            request.cookies.get(SOURCE_MEDIA_COOKIE, ""),
            scope,
        )
        result = source_monitor(channel_profile_id, project_id, run_id, candidate_id)
        return _stream_object(
            request,
            str(result["storage_key"]),
            f"editorial-source-{result['clip_id']}.{result['extension']}",
        )
    except SecretConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/{project_id}/runs/{run_id}/frames/{shot_index}")
def cited_frame_info(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    shot_index: int,
    request: Request,
):
    from fastapi.responses import JSONResponse

    from katcha.editorial.frame_inspection import inspect_frame

    _authorize(request, channel_profile_id)
    try:
        result = inspect_frame(channel_profile_id, project_id, run_id, shot_index)
        return JSONResponse(
            {key: value for key, value in result.items() if key != "image_key"},
            headers={"Cache-Control": "no-store"},
        )
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{project_id}/runs/{run_id}/frames/{shot_index}/image")
def cited_frame_image(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    shot_index: int,
    request: Request,
    evidence_digest: str = Query(pattern=r"^[a-f0-9]{64}$"),
):
    from katcha.api.studio import _stream_object
    from katcha.editorial.frame_inspection import inspect_frame

    _authorize(request, channel_profile_id)
    try:
        result = inspect_frame(channel_profile_id, project_id, run_id, shot_index)
        if result["evidence_digest"] != evidence_digest:
            raise EditorialConflict("Frame evidence changed; reload this inspection")
        response = _stream_object(request, result["image_key"], f"editorial-frame-{shot_index}.jpg")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Type"] = "image/jpeg"
        return response
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/{project_id}/runs/{run_id}/preview")
def preview_render(
    channel_profile_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID, request: Request
):
    """Authenticated review playback; clearance is checked again at access time."""
    from katcha.api.studio import _stream_object
    from katcha.services.editorial_reviews import verified_manifest
    from katcha.services.editorial_runs import get_run

    _authorize(request, channel_profile_id)
    try:
        row = get_run(channel_profile_id, project_id, run_id)
        manifest = verified_manifest(row)
        return _stream_object(request, manifest.output_key, f"editorial-{project_id}.mp4")
    except ValueError as exc:
        raise _error(exc) from exc
