"""Read-only production R2 Content-video verification fixtures and failure tests."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from fractions import Fraction
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.clip_lifecycle_models import ClipLifecycle
from katcha.content_models import ContentItem
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, SourceItem
from katcha.ops import verify_content_upload as evidence


class MemoryR2:
    def __init__(self, data: bytes, key: str) -> None:
        self.settings = SimpleNamespace(
            s3_bucket="katcha-media-prod",
            s3_endpoint_url="https://fixture.r2.cloudflarestorage.com",
        )
        self.blob = data
        self.key = key
        self.stat_calls = 0
        self.stream_calls = 0
        self.on_end = None
        self.rejected = False

    def stat(self, key):
        self.stat_calls += 1
        assert key == self.key
        if self.rejected:
            raise RuntimeError("secret-r2-key-leak-should-not-appear")
        return {"size_bytes": len(self.blob)}

    def iter_bytes(self, key, chunk_size=1024 * 1024):
        self.stream_calls += 1
        assert key == self.key
        stream = io.BytesIO(self.blob)
        while piece := stream.read(chunk_size):
            yield piece
        if self.on_end:
            self.on_end()


@pytest.fixture
def uploaded(monkeypatch, tmp_path):
    av = pytest.importorskip("av")
    media = tmp_path / "real-fixture.mkv"
    with av.open(str(media), "w") as container:
        stream = container.add_stream("ffv1", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 36, "yuv444p"
        for index in range(20):
            frame = av.VideoFrame.from_image(
                Image.new("RGB", (64, 36), "red" if index < 10 else "blue")
            )
            frame.pts = index + 50
            frame.time_base = Fraction(1, 10)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    data = media.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    channel, other, clip_id, source_id, content_id = (uuid.uuid4() for _ in range(5))
    key = f"raw/{digest[:2]}/{digest}.mkv"
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    with db.session_scope() as session:
        for ident in (channel, other):
            session.add(
                ChannelProfile(
                    id=ident,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
            )
        session.add(
            Clip(
                id=clip_id,
                sha256=digest,
                storage_key=key,
                extension="mkv",
                status="ingested",
                size_bytes=len(data),
                duration_seconds=2,
                width=64,
                height=36,
                media_metadata={},
            )
        )
        session.add(
            SourceItem(
                id=source_id,
                source_url=f"katcha-file://{content_id}",
                canonical_url=f"katcha-file://{content_id}",
                platform="local_file",
                status="ready",
                clip_id=clip_id,
                source_metadata={
                    "channel_profile_id": str(channel),
                    "content_item_id": str(content_id),
                },
            )
        )
        session.add(
            ContentItem(
                id=content_id,
                channel_profile_id=channel,
                request_key="real-owned-upload",
                input_kind="file",
                title="Golden footage",
                status="ready",
                clip_id=clip_id,
                source_id=source_id,
                filename="real-fixture.mkv",
                size_bytes=len(data),
                upload_offset=len(data),
                details={
                    "rights_confirmed": True,
                    "rights_confirmed_by": "operator",
                    "verified_sha256": digest,
                },
            )
        )
    store = MemoryR2(data, key)
    yield SimpleNamespace(
        channel=channel,
        other=other,
        clip=clip_id,
        source=source_id,
        content=content_id,
        digest=digest,
        data=data,
        store=store,
    )
    engine.dispose()


def _verify(fixture, **options):
    return evidence.verify_content_upload(
        fixture.channel,
        fixture.content,
        store=fixture.store,
        **options,
    )


def test_actual_video_bytes_verify_and_report_nonproduction_rights(uploaded):
    result = _verify(uploaded, expected_sha256=uploaded.digest)
    assert result["status"] == "verified"
    assert result["verification_level"] == "live_r2_bytes_and_ffprobe_read_only"
    assert result["sha256"] == uploaded.digest
    assert result["size_bytes"] == len(uploaded.data)
    assert result["video"]["duration_seconds"] == pytest.approx(2.0, abs=0.2)
    assert result["video"]["width"] == 64
    assert result["video"]["height"] == 36
    assert result["original_sha256_compared"] is True
    assert result["editorial_index_size_eligible"] is True
    assert result["permission_recorded"] is True
    assert result["rights_independently_verified"] is False
    assert result["production_cleared"] is False
    assert result["real_editorial_index_executed"] is False
    assert result["real_remotion_render_verified"] is False
    assert "object_key" not in result
    assert "endpoint_url" not in result
    assert uploaded.store.stat_calls == uploaded.store.stream_calls == 1


@pytest.mark.parametrize(
    "tamper",
    [
        "content_status", "incomplete", "content_kind", "content_source_id",
        "source_status", "source_platform", "source_url", "source_clip_id",
        "source_channel", "source_content_id", "receipt_permission", "receipt_actor",
        "receipt_sha", "clip_digest", "clip_size", "clip_status", "archived",
        "content_other_channel",
    ],
)
def test_missing_or_false_intake_evidence_blocks_before_r2(uploaded, tamper):
    with db.session_scope() as session:
        row = session.get(ContentItem, uploaded.content)
        source = session.get(SourceItem, uploaded.source)
        clip = session.get(Clip, uploaded.clip)
        receipt = dict(row.details)
        metadata = dict(source.source_metadata)
        if tamper == "content_status":
            row.status = "uploading"
        elif tamper == "incomplete":
            row.upload_offset -= 1
        elif tamper == "content_kind":
            row.input_kind = "url"
        elif tamper == "content_source_id":
            row.source_id = uuid.uuid4()
        elif tamper == "source_status":
            source.status = "failed"
        elif tamper == "source_platform":
            source.platform = "upload"
        elif tamper == "source_url":
            source.source_url = "katcha-file://different"
        elif tamper == "source_clip_id":
            source.clip_id = uuid.uuid4()
        elif tamper == "source_channel":
            metadata["channel_profile_id"] = str(uploaded.other)
        elif tamper == "source_content_id":
            metadata["content_item_id"] = str(uuid.uuid4())
        elif tamper == "receipt_permission":
            receipt["rights_confirmed"] = False
        elif tamper == "receipt_actor":
            receipt["rights_confirmed_by"] = ""
        elif tamper == "receipt_sha":
            receipt["verified_sha256"] = "f" * 64
        elif tamper == "clip_digest":
            clip.sha256 = "f" * 64
        elif tamper == "clip_size":
            clip.size_bytes = len(uploaded.data) - 1
        elif tamper == "clip_status":
            clip.status = "failed"
        elif tamper == "archived":
            session.add(ClipLifecycle(clip_id=clip.id, lifecycle_state="archived"))
        elif tamper == "content_other_channel":
            row.channel_profile_id = uploaded.other
        row.details = receipt
        source.source_metadata = metadata
    with pytest.raises(evidence.VerificationBlocked):
        _verify(uploaded)
    assert uploaded.store.stat_calls == 0
    assert uploaded.store.stream_calls == 0


def test_wrong_user_channel_isolated_before_storage(uploaded):
    with pytest.raises(evidence.VerificationBlocked, match="content_not_found_in_channel"):
        evidence.verify_content_upload(uploaded.other, uploaded.content, store=uploaded.store)
    assert uploaded.store.stat_calls == 0


def test_unknown_expected_hash_never_claims_success(uploaded):
    with pytest.raises(evidence.VerificationBlocked, match="expected_sha256"):
        _verify(uploaded, expected_sha256="0" * 64)
    assert uploaded.store.stat_calls == 0


def test_wrong_r2_bucket_or_endpoint_blocks_without_calls(uploaded):
    uploaded.store.settings.s3_bucket = "katcha-backup-prod"
    with pytest.raises(evidence.VerificationBlocked, match="not_configured"):
        _verify(uploaded)
    uploaded.store.settings.s3_bucket = "katcha-media-prod"
    uploaded.store.settings.s3_endpoint_url = "http://127.0.0.1:9000"
    with pytest.raises(evidence.VerificationBlocked, match="not_configured"):
        _verify(uploaded)
    assert uploaded.store.stat_calls == 0


def test_r2_missing_or_unavailable_blocks(uploaded):
    uploaded.store.rejected = True
    with pytest.raises(evidence.VerificationBlocked, match="r2_object_stat_unavailable"):
        _verify(uploaded)
    assert uploaded.store.stream_calls == 0


def test_r2_byte_tampering_never_passes_even_when_size_matches(uploaded):
    uploaded.store.blob = b"X" + uploaded.store.blob[1:]
    with pytest.raises(evidence.VerificationBlocked, match="r2_bytes_or_digest_mismatch"):
        _verify(uploaded)


def test_r2_truncated_bytes_never_pass(uploaded):
    uploaded.store.blob = uploaded.store.blob[:-3]
    with pytest.raises(evidence.VerificationBlocked, match="r2_object_size_mismatch"):
        _verify(uploaded)


def test_rights_change_during_stream_fails_closed(uploaded):
    def revoke():
        with db.session_scope() as session:
            row = session.get(ContentItem, uploaded.content)
            updated = dict(row.details)
            updated["rights_confirmed"] = False
            row.details = updated

    uploaded.store.on_end = revoke
    with pytest.raises(evidence.VerificationBlocked, match="evidence"):
        _verify(uploaded)


def test_stored_video_metadata_mismatch_blocks_even_with_correct_bytes(uploaded):
    with db.session_scope() as session:
        session.get(Clip, uploaded.clip).width = 1280
    with pytest.raises(evidence.VerificationBlocked, match="video_probe_does_not_match"):
        _verify(uploaded)



def _as_editorial_upload(uploaded, *, multipart: bool) -> None:
    with db.session_scope() as session:
        source = session.get(SourceItem, uploaded.source)
        source.platform = "upload"
        source.source_url = f"https://upload.katcha.invalid/{source.id}"
        source.canonical_url = source.source_url
        metadata = {
            "channel_profile_id": str(uploaded.channel),
            "permitted_use_confirmed": True,
            "original_filename": "real-fixture.mkv",
            "actor": "fixture-operator",
        }
        if multipart:
            metadata.update({
                "multipart": True,
                "state": "ready",
                "sha256": uploaded.digest,
                "size_bytes": len(uploaded.data),
                "part_count": 1,
                "part_bytes": len(uploaded.data),
            })
        else:
            metadata["upload_request_sha256"] = "1" * 64
        source.source_metadata = metadata


@pytest.mark.parametrize("multipart", [False, True])
def test_editorial_uploaded_video_verifies_without_content_receipt(uploaded, multipart):
    _as_editorial_upload(uploaded, multipart=multipart)
    result = evidence.verify_editorial_upload(
        uploaded.channel, uploaded.source, store=uploaded.store
    )
    assert result["status"] == "verified"
    assert result["upload_kind"] == "editorial"
    assert result["content_id"] is None
    assert result["sha256"] == uploaded.digest
    assert result["rights_independently_verified"] is False


@pytest.mark.parametrize(
    "tamper",
    ["channel", "permission", "actor", "status", "source_url", "clip_id", "multipart_sha"],
)
def test_forged_editorial_source_fails_before_r2(uploaded, tamper):
    _as_editorial_upload(uploaded, multipart=True)
    with db.session_scope() as session:
        source = session.get(SourceItem, uploaded.source)
        meta = dict(source.source_metadata)
        if tamper == "channel":
            meta["channel_profile_id"] = str(uploaded.other)
        elif tamper == "permission":
            meta["permitted_use_confirmed"] = False
        elif tamper == "actor":
            meta["actor"] = ""
        elif tamper == "status":
            meta["state"] = "verifying"
        elif tamper == "source_url":
            source.source_url = "https://example.test/unknown"
        elif tamper == "clip_id":
            source.clip_id = uuid.uuid4()
        elif tamper == "multipart_sha":
            meta["sha256"] = "0" * 64
        source.source_metadata = meta
    with pytest.raises(evidence.VerificationBlocked):
        evidence.verify_editorial_upload(
            uploaded.channel, uploaded.source, store=uploaded.store
        )
    assert uploaded.store.stat_calls == 0


def test_editorial_source_permissions_revoked_during_stream(uploaded):
    _as_editorial_upload(uploaded, multipart=False)

    def revoke():
        with db.session_scope() as session:
            source = session.get(SourceItem, uploaded.source)
            meta = dict(source.source_metadata)
            meta["permitted_use_confirmed"] = False
            source.source_metadata = meta

    uploaded.store.on_end = revoke
    with pytest.raises(evidence.VerificationBlocked, match="evidence"):
        evidence.verify_editorial_upload(
            uploaded.channel, uploaded.source, store=uploaded.store
        )


def test_cli_chooses_source_verification(monkeypatch, uploaded, capsys):
    def source_check(channel_id, source_id, *, expected_sha256):
        assert channel_id == uploaded.channel
        assert source_id == uploaded.source
        assert expected_sha256 == uploaded.digest
        return {"status": "verified"}

    monkeypatch.setattr(evidence, "verify_editorial_upload", source_check)
    assert evidence.main(
        [
            "--channel-id", str(uploaded.channel),
            "--source-id", str(uploaded.source),
            "--expected-sha256", uploaded.digest,
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "verified"}


def test_cli_does_not_disclose_provider_or_database_exceptions(monkeypatch, capsys):
    def leak(*args, **kwargs):
        raise RuntimeError("postgres://sensitive:password@host and signed R2 URL")

    monkeypatch.setattr(evidence, "verify_content_upload", leak)
    result = evidence.main(
        ["--channel-id", str(uuid.uuid4()), "--content-id", str(uuid.uuid4())]
    )
    report = capsys.readouterr()
    assert result == 3
    assert json.loads(report.out)["code"] == "check_unavailable"
    assert "password" not in report.out + report.err
    assert "signed R2" not in report.out + report.err


def test_zero_and_overlimit_receipts_fail_before_r2(uploaded):
    with db.session_scope() as session:
        row = session.get(ContentItem, uploaded.content)
        row.size_bytes = evidence.MAX_VERIFY_BYTES + 1
        row.upload_offset = row.size_bytes
    with pytest.raises(evidence.VerificationBlocked, match="outside_verify_budget"):
        _verify(uploaded)
    assert uploaded.store.stat_calls == 0
