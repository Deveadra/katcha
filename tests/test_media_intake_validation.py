from __future__ import annotations

import hashlib
import subprocess
import uuid
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate
from katcha.editorial.media_intake.validation import validate_acquired_media
from katcha.editorial_models import AcquisitionReceipt
from katcha.intelligence_models import ChannelProfile


class MemoryStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def stat(self, key: str):
        return {"size_bytes": len(self.objects[key]), "content_type": "application/octet-stream"}

    def download_file(self, key: str, destination: Path) -> None:
        destination.write_bytes(self.objects[key])


@pytest.fixture
def validation_db(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channels = [uuid.uuid4(), uuid.uuid4()]
    candidates = [uuid.uuid4(), uuid.uuid4()]
    with db.session_scope() as session:
        for channel_id, candidate_id in zip(channels, candidates, strict=True):
            session.add(
                ChannelProfile(
                    id=channel_id,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
            )
            session.add(
                DiscoveryCandidate(
                    id=candidate_id,
                    adapter_key="test",
                    external_id=uuid.uuid4().hex,
                    source_url=f"https://media.example/{candidate_id}",
                    canonical_url=f"https://media.example/canonical/{candidate_id}",
                    platform="web",
                    candidate_metadata={"channel_profile_id": str(channel_id)},
                )
            )
    yield engine, channels, candidates
    engine.dispose()


def add_receipt(engine, channel_id: uuid.UUID, candidate_id: uuid.UUID, data: bytes, mime: str):
    receipt_id = uuid.uuid4()
    digest = hashlib.sha256(data).hexdigest()
    key = f"media/sha256/{digest}"
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add(
            AcquisitionReceipt(
                id=receipt_id,
                channel_profile_id=channel_id,
                candidate_id=candidate_id,
                operation_key=f"op-{receipt_id}",
                request_digest=hashlib.sha256(receipt_id.bytes).hexdigest(),
                status="succeeded",
                authorization_scope={"evidence_scope": "authorized fixture"},
                fetch_url_if_allowed="https://media.example/source",
                request_time=datetime.now(UTC),
                response_time=datetime.now(UTC),
                bytes_received=len(data),
                sha256=digest,
                mime=mime,
                media_probe={},
                object_store_key=key,
                download_terms={},
                cost_receipt={},
                safe_resume_cursor={},
            )
        )
        session.commit()
    return receipt_id, key


def image_bytes(*, optimize: bool = False, color: tuple[int, int, int] = (120, 40, 20)) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, format="PNG", optimize=optimize)
    return buffer.getvalue()


def audio_bytes() -> bytes:
    from io import BytesIO

    output = BytesIO()
    samples = array("h", [500] * 3200)
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(samples.tobytes())
    return output.getvalue()


def video_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x24:d=0.2",
            "-an",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    return path.read_bytes()


@pytest.mark.parametrize(
    ("media_type", "mime", "build"),
    [
        ("image", "image/png", lambda tmp: image_bytes()),
        ("audio", "audio/wav", lambda tmp: audio_bytes()),
        ("video", "video/mp4", video_bytes),
    ],
)
def test_validates_actual_image_audio_and_video_content(
    validation_db, tmp_path, media_type, mime, build
):
    engine, channels, candidates = validation_db
    data = build(tmp_path)
    receipt_id, key = add_receipt(engine, channels[0], candidates[0], data, mime)
    store = MemoryStore()
    store.objects[key] = data
    result = validate_acquired_media(channels[0], receipt_id, expected_type=media_type, store=store)
    assert result.status == "validated"
    assert result.actual_sha256 == hashlib.sha256(data).hexdigest()
    assert result.size_bytes == len(data)
    if media_type == "video":
        assert result.duration_seconds == pytest.approx(0.2, abs=0.1)
        assert (result.width, result.height) == (32, 24)
    elif media_type == "audio":
        assert result.duration_seconds == pytest.approx(0.2, abs=0.1)
    else:
        assert (result.width, result.height) == (32, 24)
        assert result.perceptual_hash is not None


def test_exact_and_perceptual_duplicates_are_channel_scoped(validation_db):
    engine, channels, candidates = validation_db
    store = MemoryStore()
    first_data = image_bytes()
    first_id, first_key = add_receipt(
        engine, channels[0], candidates[0], first_data, "image/png"
    )
    store.objects[first_key] = first_data
    first = validate_acquired_media(channels[0], first_id, expected_type="image", store=store)

    exact_id, exact_key = add_receipt(
        engine, channels[0], candidates[0], first_data, "image/png"
    )
    store.objects[exact_key] = first_data
    exact = validate_acquired_media(channels[0], exact_id, expected_type="image", store=store)
    assert exact.duplicate_of_id == first.id

    near_data = image_bytes(optimize=True)
    assert near_data != first_data
    near_id, near_key = add_receipt(
        engine, channels[0], candidates[0], near_data, "image/png"
    )
    store.objects[near_key] = near_data
    near = validate_acquired_media(channels[0], near_id, expected_type="image", store=store)
    assert near.status == "validated"
    assert near.duplicate_of_id == first.id

    other_id, other_key = add_receipt(
        engine, channels[1], candidates[1], first_data, "image/png"
    )
    store.objects[other_key] = first_data
    other = validate_acquired_media(channels[1], other_id, expected_type="image", store=store)
    assert other.status == "validated"
    assert other.duplicate_of_id is None


def test_changed_bytes_invalidate_prior_validation_and_wrong_type_is_rejected(validation_db):
    engine, channels, candidates = validation_db
    store = MemoryStore()
    original = image_bytes()
    receipt_id, key = add_receipt(
        engine, channels[0], candidates[0], original, "image/png"
    )
    store.objects[key] = original
    first = validate_acquired_media(channels[0], receipt_id, expected_type="image", store=store)
    assert first.status == "validated"

    changed = image_bytes(color=(1, 200, 2))
    store.objects[key] = changed
    second = validate_acquired_media(channels[0], receipt_id, expected_type="image", store=store)
    assert second.status == "rejected"
    assert second.probe_version == first.probe_version + 1
    assert second.actual_sha256 != second.expected_sha256
    assert second.error_category == "content_hash_mismatch"

    invalid_id, invalid_key = add_receipt(
        engine, channels[0], candidates[0], b"not an image", "application/octet-stream"
    )
    store.objects[invalid_key] = b"not an image"
    invalid = validate_acquired_media(
        channels[0], invalid_id, expected_type="image", store=store
    )
    assert invalid.status == "rejected"
    assert invalid.error_category == "content_probe_failed"


def test_validation_requires_successful_channel_owned_receipt(validation_db):
    engine, channels, candidates = validation_db
    data = image_bytes()
    receipt_id, key = add_receipt(engine, channels[0], candidates[0], data, "image/png")
    store = MemoryStore()
    store.objects[key] = data
    with pytest.raises(ValueError, match="channel-owned"):
        validate_acquired_media(channels[1], receipt_id, expected_type="image", store=store)
