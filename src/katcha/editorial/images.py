"""Permission-attested stills with bounded decode, immutable receipts and revocation."""

from __future__ import annotations

import hashlib
import io
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import Field
from sqlalchemy import select, update

from katcha.db import session_scope
from katcha.editorial.narration import _project
from katcha.editorial.project_schemas import Contract, EditorialDraft, Identity, RequestKey
from katcha.editorial.visual_schemas import RenderImage
from katcha.editorial_models import EditorialImage, EditorialProject, EditorialRevision
from katcha.integrations.storage import ObjectStore
from katcha.models import DomainEvent
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest

MAX_IMAGE_BYTES = 16 * 1024 * 1024


class ImageUpload(Contract):
    revision: int = Field(gt=0, strict=True)
    beat_id: Identity
    idempotency_key: RequestKey
    title: str = Field(min_length=1, max_length=200)
    source_reference: str = Field(min_length=1, max_length=2000)
    use_note: str = Field(min_length=1, max_length=2000)
    illustration: bool = False
    permitted_use: bool = False


def normalize_image(data: bytes) -> tuple[bytes, int, int]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Choose a PNG or JPEG no larger than 16 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (
                    image.format not in {"PNG", "JPEG"}
                    or getattr(image, "n_frames", 1) != 1
                    or max(image.size) > 8192
                    or image.width * image.height > 16_000_000
                ):
                    raise ValueError(
                        "Use a still PNG/JPEG up to 8192 pixels per side and 16 megapixels"
                    )
                image.load()
                oriented = ImageOps.exif_transpose(image).convert("RGBA")
                # A new pixel-only image strips EXIF, profiles, comments and executable metadata.
                clean = Image.frombytes("RGBA", oriented.size, oriented.tobytes())
                result = io.BytesIO()
                clean.save(result, format="PNG")
                encoded = result.getvalue()
                if len(encoded) > MAX_IMAGE_BYTES:
                    raise ValueError("Decoded image is too large; reduce its dimensions")
                return encoded, clean.width, clean.height
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("The file is not a readable, bounded PNG or JPEG") from exc


def image_response(row: EditorialImage) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "revision",
            "beat_id",
            "width",
            "height",
            "title",
            "source_reference",
            "use_note",
            "illustration",
            "status",
            "actor",
            "created_at",
        )
    }


def list_images(channel_id, project_id, revision) -> dict:
    with session_scope() as session:
        _project(session, channel_id, project_id)
        rows = session.scalars(
            select(EditorialImage)
            .where(
                EditorialImage.project_id == project_id,
                EditorialImage.channel_profile_id == channel_id,
                EditorialImage.revision == revision,
            )
            .order_by(EditorialImage.created_at.desc(), EditorialImage.id)
            .limit(500)
        )
        return {"images": [image_response(row) for row in rows]}


def import_image(channel_id, project_id, *, request: ImageUpload, data: bytes, actor: str) -> dict:
    if not request.permitted_use or not all(
        value.strip()
        for value in (
            request.title,
            request.source_reference,
            request.use_note,
        )
    ):
        raise ValueError(
            "Record the image source, permitted use and explicit permission confirmation"
        )
    request_digest = _digest(
        {
            "request": request.model_dump(mode="json"),
            "actor": actor,
            "input_sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    identity = uuid.uuid5(project_id, f"image:{request.idempotency_key}")

    def validate(session):
        project = _project(session, channel_id, project_id)
        previous = session.get(EditorialImage, identity)
        if previous:
            if previous.request_digest != request_digest:
                raise EditorialConflict(
                    "Upload identity was already used for another image or permission"
                )
            return previous
        if project.revision != request.revision:
            raise EditorialConflict("Script changed. Reload before attaching an image")
        saved = session.get(EditorialRevision, (project_id, request.revision))
        draft = EditorialDraft.model_validate(saved.draft) if saved else EditorialDraft()
        if request.beat_id not in {beat.id for beat in draft.script}:
            raise EditorialConflict("Select a beat from the saved script")
        return None

    with session_scope() as session:
        previous = validate(session)
        if previous:
            return image_response(previous)
    encoded, width, height = normalize_image(data)
    digest = hashlib.sha256(encoded).hexdigest()
    key = f"editorial/{project_id}/images/{identity}/{digest}.png"
    ObjectStore().put_bytes(encoded, key, content_type="image/png")
    with session_scope() as session:
        session.execute(
            update(EditorialProject)
            .where(
                EditorialProject.id == project_id,
                EditorialProject.channel_profile_id == channel_id,
            )
            .values(updated_at=EditorialProject.updated_at)
        )
        previous = validate(session)
        if previous:
            return image_response(previous)
        row = EditorialImage(
            id=identity,
            project_id=project_id,
            channel_profile_id=channel_id,
            revision=request.revision,
            beat_id=request.beat_id,
            request_digest=request_digest,
            sha256=digest,
            storage_key=key,
            width=width,
            height=height,
            title=request.title.strip(),
            source_reference=request.source_reference.strip(),
            use_note=request.use_note.strip(),
            illustration=request.illustration,
            status="active",
            actor=actor,
        )
        session.add(row)
        session.add(
            DomainEvent(
                aggregate_type="editorial_project",
                aggregate_id=str(project_id),
                event_type="editorial.image_imported",
                payload={
                    "channel_profile_id": str(channel_id),
                    "image_id": str(identity),
                    "actor": actor,
                    "permitted_use_confirmed": True,
                    "sha256": digest,
                    "request_digest": request_digest,
                },
            )
        )
        session.flush()
        session.refresh(row)
        return image_response(row)


def resolve_images(session, channel_id, project_id, revision, plan) -> list[RenderImage]:
    result = []
    for visual in plan.beats:
        if visual.image_id is None:
            continue
        row = session.get(EditorialImage, visual.image_id)
        if row is None or (row.project_id, row.channel_profile_id) != (project_id, channel_id):
            raise EditorialNotFound("Image not found in this project")
        if row.status != "active" or row.revision != revision or row.beat_id != visual.beat_id:
            raise EditorialConflict(
                "Image was removed or belongs to another script beat or revision"
            )
        result.append(
            RenderImage(
                image_id=row.id,
                beat_id=row.beat_id,
                storage_key=row.storage_key,
                sha256=row.sha256,
                width=row.width,
                height=row.height,
                title=row.title,
                illustration=row.illustration,
            )
        )
    return result


def revoke_image(channel_id, project_id, identity, *, actor: str) -> dict:
    with session_scope() as session:
        _project(session, channel_id, project_id)
        changed = session.execute(
            update(EditorialImage)
            .where(
                EditorialImage.id == identity,
                EditorialImage.project_id == project_id,
                EditorialImage.channel_profile_id == channel_id,
                EditorialImage.status == "active",
            )
            .values(status="revoked")
        )
        row = session.get(EditorialImage, identity)
        if row is None or (row.project_id, row.channel_profile_id) != (project_id, channel_id):
            raise EditorialNotFound("Image not found in this project")
        if changed.rowcount:
            session.add(
                DomainEvent(
                    aggregate_type="editorial_project",
                    aggregate_id=str(project_id),
                    event_type="editorial.image_revoked",
                    payload={
                        "channel_profile_id": str(channel_id),
                        "image_id": str(identity),
                        "actor": actor,
                    },
                )
            )
        return image_response(row)
