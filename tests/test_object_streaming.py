"""R2 multipart, ranged video inspection and integrity regressions (offline)."""

from __future__ import annotations

import hashlib
import io
import wave
from types import SimpleNamespace

import pytest

from katcha.integrations import object_streaming as streaming


class FakeClient:
    def __init__(self, *, retain=True):
        self.retain = retain
        self.uploads = {}
        self.objects = {}
        self.aborted = []
        self.parts_seen = []
        self.calls = []

    def create_multipart_upload(self, **params):
        key = params["Key"]
        self.uploads[key] = {}
        return {"UploadId": "fixture-upload"}

    def upload_part(self, **params):
        value = params["Body"]
        self.parts_seen.append(len(value))
        self.uploads[params["Key"]][params["PartNumber"]] = (
            bytes(value) if self.retain else len(value)
        )
        return {"ETag": f'etag-{params["PartNumber"]}'}

    def complete_multipart_upload(self, **params):
        parts = self.uploads.pop(params["Key"])
        self.objects[params["Key"]] = (
            b"".join(parts[i] for i in sorted(parts))
            if self.retain else sum(parts.values())
        )

    def abort_multipart_upload(self, **params):
        self.aborted.append(params["Key"])
        self.uploads.pop(params["Key"], None)

    def head_object(self, **params):
        obj = self.objects[params["Key"]]
        return {"ContentLength": len(obj) if isinstance(obj, bytes) else obj}

    def get_object(self, **params):
        blob = self.objects[params["Key"]]
        assert isinstance(blob, bytes)
        if "Range" not in params:
            return {"Body": io.BytesIO(blob)}
        prefix, last = params["Range"].removeprefix("bytes=").split("-")
        begin, end = int(prefix), int(last)
        self.calls.append((begin, end))
        return {
            "ContentRange": f"bytes {begin}-{end}/{len(blob)}",
            "Body": io.BytesIO(blob[begin : end + 1]),
        }


class FakeStore:
    def __init__(self, client):
        self.client = client
        self.settings = SimpleNamespace(s3_bucket="media-test")

    def stat(self, key):
        return {"size_bytes": self.client.head_object(Key=key)["ContentLength"]}

    def iter_bytes(self, key, chunk_size=1024 * 1024):
        body = self.client.objects[key]
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]


@pytest.fixture(autouse=True)
def allow_fixture_mutations(monkeypatch):
    monkeypatch.setattr(streaming, "assert_mutation_authority", lambda *a, **kw: None)


def test_streaming_multipart_preserves_exact_parts_and_integrity():
    fake = FakeClient()
    store = FakeStore(fake)
    writer = streaming.MultipartObjectWriter(store, "staging/trailer.mp4")
    data = (b"abcd" * (streaming.PART_BYTES // 4)) + (b"end" * 17)
    for pos in range(0, len(data), 1024 * 1024):
        writer.write(data[pos : pos + 1024 * 1024])
    assert writer.finish() == len(data)
    assert fake.parts_seen == [streaming.PART_BYTES, 51]
    assert fake.objects["staging/trailer.mp4"] == data
    streaming.verify_stored_sha256(
        store, "staging/trailer.mp4", hashlib.sha256(data).hexdigest(), len(data)
    )
    assert fake.aborted == []


def test_three_gib_reference_keeps_only_one_eight_mib_part_in_memory():
    fake = FakeClient(retain=False)
    store = FakeStore(fake)
    writer = streaming.MultipartObjectWriter(store, "staging/3gib.mp4")
    payload = b"x" * streaming.PART_BYTES
    for _ in range(384):  # Exactly 3 GiB; object-store fixture retains sizes, not bytes.
        writer.write(payload)
        assert len(writer.buffer) <= streaming.PART_BYTES
    assert writer.finish() == 3 * 1024**3
    assert len(fake.parts_seen) == 384
    assert max(fake.parts_seen) == streaming.PART_BYTES
    assert fake.uploads == {}
    assert fake.objects["staging/3gib.mp4"] == 3 * 1024**3


def test_abort_cancels_incomplete_upload_and_does_not_publish_object():
    fake = FakeClient()
    writer = streaming.MultipartObjectWriter(FakeStore(fake), "staging/partial.mp4")
    writer.write(b"partial")
    writer.abort()
    assert fake.aborted == ["staging/partial.mp4"]
    assert fake.objects == {}


def test_no_empty_part_or_replay_after_complete():
    fake = FakeClient()
    writer = streaming.MultipartObjectWriter(FakeStore(fake), "staging/small.mp4")
    with pytest.raises(ValueError, match="no new media"):
        writer.finish()
    writer.write(b"small")
    assert writer.finish() == 5
    with pytest.raises(RuntimeError, match="completed"):
        writer.write(b"duplicate")


def test_reader_spans_cache_windows_and_reads_tail_without_full_file(monkeypatch):
    monkeypatch.setattr(streaming, "READ_AHEAD_BYTES", 16)
    data = bytes(range(100))
    fake = FakeClient()
    fake.objects["clip.mp4"] = data
    store = FakeStore(fake)
    with streaming.SeekableObjectReader(store, "clip.mp4", len(data)) as reader:
        assert reader.read(7) == data[:7]
        assert reader.read(20) == data[7:23]
        assert reader.seek(-4, io.SEEK_END) == 96
        assert reader.read(10) == data[-4:]
        reader.seek(0)
        assert reader.read(20) == data[:16]
        assert reader.read(20) == data[16:32]
    assert fake.calls[0] == (0, 15)
    assert fake.calls[-1] == (16, 31)


def test_reader_rejects_wrong_range_and_different_stored_hash():
    fake = FakeClient()
    fake.objects["clip.mp4"] = b"original media"
    store = FakeStore(fake)
    streaming.verify_stored_sha256(
        store, "clip.mp4", hashlib.sha256(b"original media").hexdigest(), 14
    )
    with pytest.raises(ValueError, match="SHA-256"):
        streaming.verify_stored_sha256(store, "clip.mp4", "0" * 64, 14)
    original = fake.get_object

    def wrong_range(**params):
        response = original(**params)
        response["ContentRange"] = "bytes 0-0/0"
        return response

    fake.get_object = wrong_range
    with (
        streaming.SeekableObjectReader(store, "clip.mp4", 14) as reader,
        pytest.raises(ValueError, match="ignored"),
    ):
        reader.read(2)


def test_pyav_reads_real_non_faststart_mp4_from_ranged_r2_fixture(tmp_path):
    sample = tmp_path / "fixture.mp4"
    import av
    from PIL import Image

    with av.open(str(sample), mode="w") as output:
        video = output.add_stream("mpeg4", rate=12)
        video.width, video.height = 160, 90
        video.pix_fmt = "yuv420p"
        frame = av.VideoFrame.from_image(Image.new("RGB", (160, 90), color="blue"))
        for packet in video.encode(frame):
            output.mux(packet)
        for packet in video.encode():
            output.mux(packet)
    data = sample.read_bytes()
    assert data.index(b"moov") > data.index(b"mdat")
    fake = FakeClient()
    fake.objects["clip.mp4"] = data
    result = streaming.inspect_stored_media(FakeStore(fake), "clip.mp4", len(data))
    assert result["format"]["duration"] is not None
    assert result["streams"][0]["codec_type"] == "video"
    assert result["streams"][0]["width"] == 160
    assert result["streams"][0]["height"] == 90
    assert fake.calls


def test_pyav_rejects_audio_only_stream(tmp_path):
    sample = tmp_path / "audio.wav"
    with wave.open(str(sample), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(bytes(1600))
    data = sample.read_bytes()
    fake = FakeClient()
    fake.objects["audio.wav"] = data
    with pytest.raises(ValueError, match="no decodable video"):
        streaming.inspect_stored_media(FakeStore(fake), "audio.wav", len(data))
