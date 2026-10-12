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
