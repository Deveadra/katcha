"""Offline tests for source-independent, DNS-pinned direct media downloads."""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from katcha.config import Settings
from katcha.integrations import direct_media, download as downloader
from katcha.orchestration.failure_details import failure_details


class Response:
    def __init__(self, status=200, content=b"fixture-video-bytes", headers=None):
        self.status = status
        self.content = content
        self.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(content)),
            **(headers or {}),
        }
        self.offset = 0
        self.chunk_sizes = []

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size):
        assert 0 < size <= direct_media.READ_CHUNK
        self.chunk_sizes.append(size)
        result = self.content[self.offset : self.offset + size]
        self.offset += len(result)
        return result


def fake_network(monkeypatch, *responses, hosts=None):
    calls = []
    remaining = list(responses)
    expected_hosts = hosts

    def public_addresses(url):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            raise ValueError("Only public HTTPS destinations are allowed")
        if parsed.hostname in {"127.0.0.1", "localhost", "169.254.169.254"}:
            raise ValueError("Private address rejected")
        return parsed.hostname, ["93.184.215.14"]

    class Connection:
        def __init__(self, host, address):
            self.host, self.address = host, address
            calls.append(self)
            assert address == "93.184.215.14"
            self.response = remaining.pop(0)

        def request(self, method, target, headers):
            assert method == "GET"
            assert "Authorization" not in headers
            assert "Cookie" not in headers
            assert headers["Accept-Encoding"] == "identity"
            self.target, self.headers = target, headers

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    monkeypatch.setattr(direct_media, "public_addresses", public_addresses)
    monkeypatch.setattr(direct_media, "_PinnedHTTPS", Connection)
    return calls


def test_direct_public_media_streams_and_hashes_without_extractors(monkeypatch, tmp_path):
    data = (b"video!" * 180_000) + b"end"
    response = Response(content=data)
    calls = fake_network(monkeypatch, response)

    def no_extractor(_options):
        raise AssertionError("Direct publisher footage must not invoke yt-dlp")

    monkeypatch.setattr(downloader, "YoutubeDL", no_extractor)
    monkeypatch.setattr(
        downloader, "ffprobe",
        lambda path: {"streams": [{"codec_type": "video", "width": 3840, "height": 2160}]},
    )
    result = downloader.download(
        "https://publisher.example.org/trailers/latest.mp4?signature=private-test-token",
        Settings(_env_file=None, work_dir=tmp_path),
    )
    assert result.size_bytes == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.path.read_bytes() == data
    assert result.source_metadata["acquisition_transport"] == "pinned_https_direct_file"
    assert result.source_metadata["direct_media_host"] == "publisher.example.org"
    assert result.media_metadata["streams"][0]["width"] == 3840
    assert calls[0].target.endswith("latest.mp4?signature=private-test-token")
    assert len(response.chunk_sizes) >= 2
    assert max(response.chunk_sizes) == direct_media.READ_CHUNK


def test_redirect_to_another_public_host_is_validated_and_no_credentials_forwarded(
    monkeypatch, tmp_path
):
    responses = [
        Response(status=302, headers={"Location": "https://cdn.example.net/assets/opaque?sig=private"}),
        Response(content=b"direct media"),
    ]
    calls = fake_network(monkeypatch, *responses)
    got = direct_media.fetch_direct_media(
        "https://press.example.org/media/clip.mov", tmp_path
    )
    assert [c.host for c in calls] == ["press.example.org", "cdn.example.net"]
    assert got.final_host == "cdn.example.net"
    assert got.redirects == 1
    assert got.extension == "mov"
    assert got.sha256 == hashlib.sha256(b"direct media").hexdigest()
    assert "sig=private" in calls[-1].target


@pytest.mark.parametrize(
    "target",
    [
        "http://press.example.org/media/clip.mp4",
        "https://user:secret@press.example.org/media/clip.mp4",
        "https://127.0.0.1/clip.mp4",
        "https://169.254.169.254/clip.mp4",
    ],
)
def test_bad_destination_never_creates_media(monkeypatch, tmp_path, target):
    calls = fake_network(monkeypatch)
    with pytest.raises(direct_media.DirectMediaRejected):
        direct_media.fetch_direct_media(target, tmp_path)
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_real_public_dns_validator_rejects_private_or_mixed_addresses(monkeypatch, tmp_path):
    def resolved(_host, _port, *, type):
        assert type == socket.SOCK_STREAM
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.42.2.144", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolved)
    monkeypatch.setattr(
        direct_media, "_PinnedHTTPS", lambda *_: (_ for _ in ()).throw(
            AssertionError("A private DNS answer must prevent a connection")
        ),
    )
    with pytest.raises(direct_media.DirectMediaRejected):
        direct_media.fetch_direct_media(
            "https://press.example.org/media/clip.mp4", tmp_path
        )


def test_redirect_to_localhost_blocks_followup_request(monkeypatch, tmp_path):
    calls = fake_network(
        monkeypatch,
        Response(status=302, headers={"Location": "https://127.0.0.1/internal"}),
    )
    with pytest.raises(direct_media.DirectMediaRejected):
        direct_media.fetch_direct_media(
            "https://press.example.org/media/clip.mp4", tmp_path
        )
    assert len(calls) == 1
    assert not (tmp_path / "source.mp4").exists()


@pytest.mark.parametrize(
    "response,expected_class",
    [
        (Response(content=b"<html>sign in</html>", headers={"Content-Type": "text/html"}),
         direct_media.DirectMediaRejected),
        (Response(status=403), direct_media.DirectMediaAccessDenied),
        (Response(status=404), direct_media.DirectMediaRejected),
        (Response(headers={"Content-Length": str(4 * 1024**3 + 1)}),
         direct_media.DirectMediaTooLarge),
        (Response(headers={"Content-Length": "unknown"}), direct_media.DirectMediaRejected),
        (Response(headers={"Content-Encoding": "gzip"}), direct_media.DirectMediaRejected),
    ],
)
def test_rejected_direct_files_never_persist_bytes(
    monkeypatch, tmp_path, response, expected_class
):
    fake_network(monkeypatch, response)
    with pytest.raises(expected_class):
        direct_media.fetch_direct_media(
            "https://press.example.org/media/clip.mp4", tmp_path
        )
    assert not (tmp_path / "source.mp4").exists()


def test_incomplete_file_cleans_up_and_reports_typed_transport_failure(
    monkeypatch, tmp_path
):
    fake_network(
        monkeypatch,
        Response(content=b"short", headers={"Content-Length": "12"}),
    )
    monkeypatch.setattr(
        downloader, "ffprobe",
        lambda path: (_ for _ in ()).throw(AssertionError("must not ffprobe partial bytes")),
    )
    with pytest.raises(downloader.MediaDownloadError) as caught:
        downloader.download(
            "https://press.example.org/file.mp4?access_token=private-secret",
            Settings(_env_file=None, work_dir=tmp_path),
        )
    detail = failure_details(caught.value)
    assert detail["code"] == "direct_media_transport_failed"
    assert detail["retryable"] is True
    assert "private-secret" not in str(detail)
    assert list(tmp_path.iterdir()) == []


def test_non_video_bytes_cannot_be_marked_ingested(monkeypatch, tmp_path):
    fake_network(monkeypatch, Response(content=b"audio-only"))
    monkeypatch.setattr(
        downloader, "ffprobe",
        lambda path: {"streams": [{"codec_type": "audio"}]},
    )
    with pytest.raises(downloader.MediaDownloadError) as caught:
        downloader.download(
            "https://press.example.org/file.mp4",
            Settings(_env_file=None, work_dir=tmp_path),
        )
    assert failure_details(caught.value)["code"] == "direct_media_rejected"
    assert list(tmp_path.iterdir()) == []


def test_non_media_pages_still_take_existing_extractor_route(monkeypatch, tmp_path):
    calls = []

    class Extractor:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, *_args, **_kwargs):
            raise RuntimeError("Existing extractor path executed")

    monkeypatch.setattr(downloader, "YoutubeDL", Extractor)
    with pytest.raises(downloader.MediaDownloadError):
        downloader.download(
            "https://press.example.org/press-release",
            Settings(_env_file=None, work_dir=tmp_path),
        )
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_file_detection_matches_path_not_untrusted_query():
    assert direct_media.is_direct_media_url("https://cdn.example.org/raw/trailer.MP4?sig=secret")
    assert direct_media.is_direct_media_url("https://cdn.example.org/raw/clip.webm")
    assert not direct_media.is_direct_media_url("https://cdn.example.org/watch?file=clip.mp4")
    assert not direct_media.is_direct_media_url("https://cdn.example.org/index.html")
