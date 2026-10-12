from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition_models import DiscoveryCandidate
from katcha.editorial.media_intake.contracts import ProviderPermissionV1
from katcha.editorial.media_intake.downloader import fetch_authorized_media
from katcha.intelligence_models import ChannelProfile


@pytest.fixture
def fetch_db(monkeypatch):
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel_id, candidate_id = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
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
                adapter_key="licensed-provider",
                external_id=uuid.uuid4().hex,
                source_url="https://media.example/item",
                canonical_url=f"https://media.example/{candidate_id}",
                platform="web",
                candidate_metadata={"channel_profile_id": str(channel_id)},
            )
        )
    yield channel_id, candidate_id
    engine.dispose()


class MemoryStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return key in self.objects

    def put_file(self, path, key: str, content_type: str | None = None) -> None:
        self.objects[key] = path.read_bytes()


def allow_public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type: [(None, None, None, None, ("93.184.216.34", port))],
    )


def permission() -> ProviderPermissionV1:
    return ProviderPermissionV1(
        provider="licensed-provider",
        operations={"download"},
        allowed_hosts=["media.example"],
        evidence_scope="licensed download",
        evidence_refs=["operator-attestation:123"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_allowlisted_fetch_streams_to_content_addressed_store(fetch_db, monkeypatch):
    allow_public_dns(monkeypatch)
    channel_id, candidate_id = fetch_db
    store = MemoryStore()
    authorized = permission()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"media bytes")

    def client_factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    receipt = fetch_authorized_media(
        channel_id,
        candidate_id,
        operation_key="fetch-once",
        permission=authorized,
        fetch_url="https://media.example/source.mp4",
        actor="test",
        store=store,
        client_factory=client_factory,
    )
    assert receipt.status == "succeeded"
    assert receipt.bytes_received == len(b"media bytes")
    assert receipt.object_store_key == f"media/sha256/{receipt.sha256}"
    assert store.objects[receipt.object_store_key] == b"media bytes"
    assert len(calls) == 1
    replay = fetch_authorized_media(
        channel_id,
        candidate_id,
        operation_key="fetch-once",
        permission=authorized,
        fetch_url="https://media.example/source.mp4",
        actor="test",
        store=store,
        client_factory=client_factory,
    )
    assert replay.id == receipt.id
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://media.example/source.mp4",
        "https://unlisted.example/source.mp4",
        "https://media.example/source.mp4?token=secret",
        "https://user:password@media.example/source.mp4",
        "https://media.example:8443/source.mp4",
    ],
)
def test_rejects_untrusted_or_credential_bearing_url(fetch_db, monkeypatch, url):
    allow_public_dns(monkeypatch)
    channel_id, candidate_id = fetch_db
    with pytest.raises(ValueError):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="invalid-url",
            permission=permission(),
            fetch_url=url,
            actor="test",
            store=MemoryStore(),
        )


def test_rejects_private_dns_and_redirects(fetch_db, monkeypatch):
    channel_id, candidate_id = fetch_db
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type: [(None, None, None, None, ("127.0.0.1", port))],
    )
    with pytest.raises(ValueError, match="non-public"):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="private-dns",
            permission=permission(),
            fetch_url="https://media.example/source.mp4",
            actor="test",
            store=MemoryStore(),
        )

    allow_public_dns(monkeypatch)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/metadata"})

    def client_factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    with pytest.raises(ValueError, match="allowlisted"):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="private-redirect",
            permission=permission(),
            fetch_url="https://media.example/source.mp4",
            actor="test",
            store=MemoryStore(),
            client_factory=client_factory,
        )
    assert len(calls) == 1


def test_size_cap_and_unknown_outcome_do_not_retry(fetch_db, monkeypatch):
    allow_public_dns(monkeypatch)
    channel_id, candidate_id = fetch_db
    store = MemoryStore()

    def large_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"12345")

    with pytest.raises(ValueError, match="size"):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="too-large",
            permission=permission(),
            fetch_url="https://media.example/large.mp4",
            actor="test",
            store=store,
            max_bytes=4,
            client_factory=lambda **kwargs: httpx.Client(
                transport=httpx.MockTransport(large_handler), **kwargs
            ),
        )
    assert store.objects == {}

    attempts = 0

    def broken_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("connection lost")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(broken_handler), **kwargs)

    args = {
        "operation_key": "unknown-outcome",
        "permission": permission(),
        "fetch_url": "https://media.example/source.mp4",
        "actor": "test",
        "store": store,
        "client_factory": factory,
    }
    with pytest.raises(ValueError, match="reconcile"):
        fetch_authorized_media(channel_id, candidate_id, **args)
    assert attempts == 1
    with pytest.raises(ValueError, match="unknown"):
        fetch_authorized_media(channel_id, candidate_id, **args)
    assert attempts == 1


class FakeSocket:
    def __init__(self, response: bytes, destinations: list):
        self.response = response
        self.destinations = destinations
        self.written = b""
        self.closed = False

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args):
        pass

    def connect(self, address):
        self.destinations.append(address)

    def send(self, data):
        self.written += data
        return len(data)

    def recv(self, max_bytes):
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    def fileno(self):
        return -1

    def close(self):
        self.closed = True


def fake_tls(monkeypatch, *, reject=False):
    import ssl

    import httpx._transports.default as default_transport

    context = ssl.create_default_context()
    names = []

    def wrap(sock, server_hostname):
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        names.append(server_hostname)
        if reject:
            raise ssl.SSLCertVerificationError("hostname mismatch")
        return sock

    monkeypatch.setattr(context, "wrap_socket", wrap)
    monkeypatch.setattr(httpx, "create_ssl_context", lambda **kwargs: context)
    monkeypatch.setattr(default_transport, "create_ssl_context", lambda **kwargs: context)
    return names


def test_connection_uses_dns_snapshot_without_resolving_again_and_keeps_tls_hostname(monkeypatch):
    from katcha.editorial.media_intake.transport import PublicDestinationTransport

    resolutions = []
    destinations = []
    sockets = []
    dns_changed = False

    def resolve(host, port, type):
        resolutions.append(host)
        return [(None, None, None, None, ("127.0.0.1" if dns_changed else "93.184.216.34", port))]

    def make_socket(*args):
        nonlocal dns_changed
        dns_changed = True
        sock = FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata", destinations)
        sockets.append(sock)
        return sock

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", make_socket)
    names = fake_tls(monkeypatch)
    with httpx.Client(
        transport=PublicDestinationTransport(permission()), trust_env=False
    ) as client:
        assert client.get("https://media.example/asset").content == b"data"
    assert resolutions == ["media.example"]
    assert destinations == [("93.184.216.34", 443)]
    assert names == ["media.example"]
    assert b"Host: media.example\r\n" in sockets[0].written


def test_certificate_failure_is_not_bypassed(monkeypatch):
    from katcha.editorial.media_intake.transport import PublicDestinationTransport

    allow_public_dns(monkeypatch)
    destinations = []
    sock = FakeSocket(b"", destinations)
    monkeypatch.setattr(socket, "socket", lambda *args: sock)
    names = fake_tls(monkeypatch, reject=True)
    with (
        httpx.Client(transport=PublicDestinationTransport(permission()), trust_env=False) as client,
        pytest.raises(httpx.ConnectError),
    ):
        client.get("https://media.example/asset")
    assert names == ["media.example"]
    assert sock.closed


def test_redirect_dns_rebinding_is_rejected_before_second_connection(fetch_db, monkeypatch):

    destinations = []

    def resolve(host, port, type):
        ip = "127.0.0.1" if destinations else "93.184.216.34"
        return [(None, None, None, None, (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args: FakeSocket(
            b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n", destinations
        ),
    )
    names = fake_tls(monkeypatch)
    channel_id, candidate_id = fetch_db
    with pytest.raises(ValueError, match="non-public"):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="rebind-redirect",
            permission=permission(),
            fetch_url="https://media.example/asset",
            actor="test",
            store=MemoryStore(),
        )
    assert destinations == [("93.184.216.34", 443)]
    assert names == ["media.example"]


def test_partial_storage_write_is_unknown_and_cannot_repeat(fetch_db, monkeypatch):
    from sqlalchemy import select

    from katcha.editorial_models import AcquisitionReceipt

    allow_public_dns(monkeypatch)
    channel_id, candidate_id = fetch_db
    calls = []

    class InterruptedStore(MemoryStore):
        def put_file(self, path, key, content_type=None):
            super().put_file(path, key, content_type)
            raise ValueError("storage write acknowledgement lost")

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"media bytes")

    kwargs = dict(
        operation_key="interrupted-store",
        permission=permission(),
        fetch_url="https://media.example/asset",
        actor="test",
        store=InterruptedStore(),
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    with pytest.raises(ValueError):
        fetch_authorized_media(channel_id, candidate_id, **kwargs)
    with db.session_scope() as session:
        receipt = session.scalar(select(AcquisitionReceipt))
        assert receipt.status == "unknown"
    with pytest.raises(ValueError, match="unknown"):
        fetch_authorized_media(channel_id, candidate_id, **kwargs)
    assert len(calls) == 1


def test_rebinding_between_validation_and_connect_never_connects_private(fetch_db, monkeypatch):
    resolutions = []
    destinations = []

    def resolve(host, port, *args, **kwargs):
        resolutions.append(host)
        ip = "93.184.216.34" if len(resolutions) <= 2 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args: FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata", destinations),
    )
    fake_tls(monkeypatch)
    channel_id, candidate_id = fetch_db
    with pytest.raises(ValueError, match="non-public"):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="validation-to-connect",
            permission=permission(),
            fetch_url="https://media.example/asset",
            actor="test",
            store=MemoryStore(),
        )
    assert len(resolutions) == 3
    assert destinations == []


def test_allowlisted_redirect_keeps_each_original_tls_hostname(fetch_db, monkeypatch):
    destinations = []
    socks = []
    responses = [
        b"HTTP/1.1 302 Found\r\nLocation: https://cdn.example/final\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata",
    ]

    def resolve(host, port, type):
        ip = "93.184.216.34" if host == "media.example" else "93.184.216.35"
        return [(None, None, None, None, (ip, port))]

    def make_socket(*args):
        sock = FakeSocket(responses[len(socks)], destinations)
        socks.append(sock)
        return sock

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", make_socket)
    names = fake_tls(monkeypatch)
    channel_id, candidate_id = fetch_db
    receipt = fetch_authorized_media(
        channel_id,
        candidate_id,
        operation_key="public-redirect",
        permission=permission().model_copy(
            update={"allowed_hosts": ["media.example", "cdn.example"]}
        ),
        fetch_url="https://media.example/asset",
        actor="test",
        store=MemoryStore(),
    )
    assert receipt.status == "succeeded"
    assert destinations == [("93.184.216.34", 443), ("93.184.216.35", 443)]
    assert names == ["media.example", "cdn.example"]
    assert b"Host: cdn.example\r\n" in socks[1].written


def test_store_initialization_failure_does_not_leave_receipt_acquiring(fetch_db, monkeypatch):
    from sqlalchemy import select

    from katcha.editorial_models import AcquisitionReceipt

    allow_public_dns(monkeypatch)
    channel_id, candidate_id = fetch_db

    def fail_store():
        raise OSError("storage configuration unavailable")

    monkeypatch.setattr("katcha.editorial.media_intake.downloader.ObjectStore", fail_store)
    with pytest.raises(ValueError, match="reconcile"):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="store-init",
            permission=permission(),
            fetch_url="https://media.example/asset",
            actor="test",
        )
    with db.session_scope() as session:
        assert session.scalar(select(AcquisitionReceipt)).status == "failed"


def test_interrupted_stream_removes_temp_file_and_stays_unknown(fetch_db, monkeypatch):
    import tempfile
    from pathlib import Path

    from sqlalchemy import select

    from katcha.editorial_models import AcquisitionReceipt

    allow_public_dns(monkeypatch)
    channel_id, candidate_id = fetch_db
    paths = []
    original = tempfile.NamedTemporaryFile

    def capture_temp(*args, **kwargs):
        temp = original(*args, **kwargs)
        paths.append(Path(temp.name))
        return temp

    def interrupt(response):
        yield b"partial"
        raise OSError("connection closed before completion")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", capture_temp)
    monkeypatch.setattr("katcha.editorial.media_intake.downloader._chunks", interrupt)
    store = MemoryStore()
    with pytest.raises(ValueError, match="reconcile"):
        fetch_authorized_media(
            channel_id,
            candidate_id,
            operation_key="stream-interrupted",
            permission=permission(),
            fetch_url="https://media.example/asset",
            actor="test",
            store=store,
            client_factory=lambda **kwargs: httpx.Client(
                transport=httpx.MockTransport(lambda req: httpx.Response(200)), **kwargs
            ),
        )
    assert paths and all(not path.exists() for path in paths)
    assert store.objects == {}
    with db.session_scope() as session:
        receipt = session.scalar(select(AcquisitionReceipt))
        assert receipt.status == "unknown"
        assert receipt.bytes_received == len(b"partial")


@pytest.mark.parametrize("unsafe", ["127.0.0.1", "169.254.169.254", "224.0.0.1", "ff02::1"])
def test_mixed_dns_snapshot_rejects_non_public_unicast_before_socket_creation(monkeypatch, unsafe):
    from katcha.editorial.media_intake.transport import PublicDestinationBackend

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, type: [
            (None, None, None, None, ("93.184.216.34", port)),
            (None, None, None, None, (unsafe, port)),
        ],
    )
    monkeypatch.setattr(
        socket, "socket", lambda *args: pytest.fail("Unsafe DNS must not create sockets")
    )
    with pytest.raises(ValueError, match="non-public"):
        PublicDestinationBackend(permission()).connect_tcp("media.example", 443)
