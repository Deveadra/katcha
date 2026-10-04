"""Bounded WAV intake. Measured audio is not a verified transcript or generated speech."""

from __future__ import annotations

import hashlib
import io
import uuid
import wave

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from katcha.db import session_scope
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.visual_schemas import RenderNarration, StoryboardPlan
from katcha.editorial_models import EditorialNarration, EditorialProject, EditorialRevision
from katcha.integrations.storage import ObjectStore
from katcha.models import DomainEvent
from katcha.provider_setting_models import ChannelProviderSetting
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest

MAX_AUDIO_BYTES = 32 * 1024 * 1024


def voice_enabled(session: Session, channel_id: uuid.UUID) -> bool:
    setting = session.scalar(
        select(ChannelProviderSetting).where(
            ChannelProviderSetting.channel_profile_id == channel_id,
            ChannelProviderSetting.provider == "elevenlabs",
        )
    )
    return setting is None or setting.enabled


def measure_wav(data: bytes) -> tuple[int, int]:
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise ValueError("Upload a PCM WAV recording no larger than 32 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            rate, frames = audio.getframerate(), audio.getnframes()
            channels, width = audio.getnchannels(), audio.getsampwidth()
            if (
                audio.getcomptype() != "NONE"
                or channels not in {1, 2}
                or width not in {1, 2, 3, 4}
                or not 8000 <= rate <= 96000
                or not 0 < frames <= 600 * rate
                or frames * channels * width > MAX_AUDIO_BYTES
            ):
                raise ValueError("Use a mono/stereo PCM WAV recording up to ten minutes per beat")
            if len(audio.readframes(frames)) != frames * channels * width:
                raise ValueError("The WAV recording is truncated")
            return rate, frames
    except (wave.Error, EOFError) as exc:
        raise ValueError("The file is not a readable PCM WAV recording") from exc


def narration_response(row: EditorialNarration) -> dict:
    return {
        "id": str(row.id),
        "revision": row.revision,
        "beat_id": row.beat_id,
        "duration_seconds": row.sample_frames / row.sample_rate,
        "sample_rate": row.sample_rate,
        "sample_frames": row.sample_frames,
        "status": row.status,
        "actor": row.actor,
        "created_at": row.created_at,
        "transcript_verified": False,
    }


def _project(session: Session, channel_id: uuid.UUID, project_id: uuid.UUID):
    ensure_active_profile(session, channel_id)
    project = session.get(EditorialProject, project_id)
    if project is None or project.channel_profile_id != channel_id:
        raise EditorialNotFound("Editorial project not found in this channel")
    return project


def list_narration(channel_id: uuid.UUID, project_id: uuid.UUID, revision: int) -> dict:
    with session_scope() as session:
        _project(session, channel_id, project_id)
        rows = session.scalars(
            select(EditorialNarration)
            .where(
                EditorialNarration.project_id == project_id,
                EditorialNarration.channel_profile_id == channel_id,
                EditorialNarration.revision == revision,
            )
            .order_by(EditorialNarration.created_at.desc(), EditorialNarration.id)
            .limit(500)
        )
        return {
            "voice_enabled": voice_enabled(session, channel_id),
            "recordings": [narration_response(row) for row in rows],
        }


def import_narration(
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    revision: int,
    beat_id: str,
    idempotency_key: str,
    audio: bytes,
    actor: str,
    permitted_use: bool,
) -> dict:
    if not permitted_use:
        raise ValueError("Confirm that you have permission to use this recording")
    rate, frames = measure_wav(audio)
    sha256 = hashlib.sha256(audio).hexdigest()
    identity = uuid.uuid5(project_id, f"narration:{idempotency_key}")
    key = f"editorial/{project_id}/narration/{identity}/{sha256}.wav"

    def validate(session):
        project = _project(session, channel_id, project_id)
        previous = session.get(EditorialNarration, identity)
        if previous is not None:
            if (previous.revision, previous.beat_id, previous.sha256, previous.actor) != (
                revision,
                beat_id,
                sha256,
                actor,
            ):
                raise EditorialConflict("Upload identity was already used for another recording")
            return previous, None
        if project.revision != revision:
            raise EditorialConflict("Script changed. Save and reload before attaching narration")
        if not voice_enabled(session, channel_id):
            raise EditorialConflict("Voice is off for this channel. Enable it in Channel Studio")
        saved = session.get(EditorialRevision, (project_id, revision))
        draft = EditorialDraft.model_validate(saved.draft) if saved else EditorialDraft()
        beat = next((beat for beat in draft.script if beat.id == beat_id), None)
        if beat is None:
            raise EditorialConflict("Select a beat from the saved script")
        return None, beat

    with session_scope() as session:
        previous, _ = validate(session)
        if previous is not None:
            return narration_response(previous)
    # Upload outside the DB transaction; content-addressed keys make uncertain
    # retries safe. An interrupted commit may leave an unreferenced object.
    ObjectStore().put_bytes(audio, key, content_type="audio/wav")
    with session_scope() as session:
        locked = session.execute(
            update(EditorialProject)
            .where(
                EditorialProject.id == project_id,
                EditorialProject.channel_profile_id == channel_id,
            )
            .values(updated_at=EditorialProject.updated_at)
        )
        if locked.rowcount != 1:
            raise EditorialNotFound("Editorial project not found in this channel")
        previous, beat = validate(session)
        if previous is not None:
            return narration_response(previous)
        row = EditorialNarration(
            id=identity,
            project_id=project_id,
            channel_profile_id=channel_id,
            revision=revision,
            beat_id=beat_id,
            text_digest=_digest({"text": beat.narration}),
            sha256=sha256,
            storage_key=key,
            sample_rate=rate,
            sample_frames=frames,
            status="active",
            actor=actor,
        )
        session.add(row)
        session.add(
            DomainEvent(
                aggregate_type="editorial_project",
                aggregate_id=str(project_id),
                event_type="editorial.narration_imported",
                payload={
                    "channel_profile_id": str(channel_id),
                    "narration_id": str(identity),
                    "revision": revision,
                    "beat_id": beat_id,
                    "actor": actor,
                    "permitted_use_confirmed": True,
                    "sha256": sha256,
                },
            )
        )
        session.flush()
        session.refresh(row)
        return narration_response(row)


def resolve_narration(
    session: Session,
    channel_id: uuid.UUID,
    project_id: uuid.UUID,
    revision: int,
    draft: EditorialDraft,
    plan: StoryboardPlan,
) -> list[RenderNarration]:
    if plan.presentation_mode != "narrated":
        return []
    if not voice_enabled(session, channel_id):
        raise EditorialConflict("Voice is off for this channel. Enable it in Channel Studio")
    result = []
    for beat in draft.script:
        row = session.get(EditorialNarration, plan.narration_ids.get(beat.id))
        if row is None or (row.project_id, row.channel_profile_id) != (project_id, channel_id):
            raise EditorialNotFound("Narration recording not found in this project")
        if row.status != "active" or row.revision != revision or row.beat_id != beat.id:
            raise EditorialConflict("Narration was removed or belongs to another script revision")
        if row.text_digest != _digest({"text": beat.narration}):
            raise EditorialConflict("Narration does not match the current script text")
        result.append(
            RenderNarration(
                narration_id=row.id,
                beat_id=beat.id,
                storage_key=row.storage_key,
                sha256=row.sha256,
                text_digest=row.text_digest,
                sample_rate=row.sample_rate,
                sample_frames=row.sample_frames,
            )
        )
    return result


def revoke_narration(
    channel_id: uuid.UUID, project_id: uuid.UUID, identity: uuid.UUID, *, actor: str
) -> dict:
    with session_scope() as session:
        _project(session, channel_id, project_id)
        result = session.execute(
            update(EditorialNarration)
            .where(
                EditorialNarration.id == identity,
                EditorialNarration.project_id == project_id,
                EditorialNarration.channel_profile_id == channel_id,
                EditorialNarration.status == "active",
            )
            .values(status="revoked")
        )
        row = session.get(EditorialNarration, identity)
        if row is None or row.project_id != project_id or row.channel_profile_id != channel_id:
            raise EditorialNotFound("Narration recording not found in this project")
        if result.rowcount:
            session.add(
                DomainEvent(
                    aggregate_type="editorial_project",
                    aggregate_id=str(project_id),
                    event_type="editorial.narration_revoked",
                    payload={
                        "channel_profile_id": str(channel_id),
                        "narration_id": str(identity),
                        "actor": actor,
                    },
                )
            )
        return narration_response(row)
