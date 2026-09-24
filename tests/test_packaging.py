from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from katcha import longform_models, production_models, short_episode_models  # noqa: F401
from katcha.api.main import app
from katcha.db import Base
from katcha.packaging_models import (
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.services import packaging as packaging_service


class FakeStore:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def exists(self, key: str) -> bool:
        return bool(key)

    def stat(self, key: str) -> dict[str, object]:
        return {
            "size_bytes": len(self.data),
            "content_type": "image/png",
            "etag": "etag",
        }

    def get_bytes(self, key: str) -> bytes:
        return self.data


@pytest.fixture
def packaging_scope(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session: Session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(packaging_service, "session_scope", scope)
    return scope


def _publication(scope, *, suffix: str) -> Publication:
    with scope() as session:
        connection = YouTubeConnection(
            channel_id=f"channel-{suffix}",
            channel_title=f"Channel {suffix}",
            status="active",
            scopes=["youtube"],
            encrypted_access_token="encrypted",
            encrypted_refresh_token="encrypted",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        publication = Publication(
            production_id=uuid.uuid4(),
            youtube_connection_id=connection.id,
            workflow_id=f"publication-{suffix}",
            analytics_workflow_id=f"analytics-{suffix}",
            title="Original title",
            description="Original description",
            youtube_video_id=f"video-{suffix}",
        )
        session.add(publication)
        session.flush()
        session.refresh(publication)
        session.expunge(publication)
        return publication


def test_packaging_models_enforce_variant_and_activation_identity() -> None:
    variant_unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in PublicationPackagingVariant.__table__.constraints
        if hasattr(constraint, "columns")
    }
    activation_unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in PublicationPackagingActivation.__table__.constraints
        if hasattr(constraint, "columns")
    }
    assert ("publication_id", "variant_key", "version") in variant_unique
    assert ("publication_id", "activation_key") in activation_unique


def test_variant_freezes_thumbnail_hash_and_is_idempotent(packaging_scope) -> None:
    publication = _publication(packaging_scope, suffix="one")
    data = b"\x89PNG\r\n\x1a\nreal-thumbnail-bytes"
    key = f"packaging/{publication.id}/hook-a/v1/thumbnail.png"
    first = packaging_service.create_packaging_variant(
        publication.id,
        variant_key="hook-a",
        version=1,
        title="A much stronger hook",
        description="Description",
        thumbnail_storage_key=key,
        created_by="test",
        store=FakeStore(data),
    )
    second = packaging_service.create_packaging_variant(
        publication.id,
        variant_key="hook-a",
        version=1,
        title="A much stronger hook",
        description="Description",
        thumbnail_storage_key=key,
        created_by="test",
        store=FakeStore(data),
    )
    assert second.id == first.id
    assert first.thumbnail_content_type == "image/png"
    assert first.thumbnail_size_bytes == len(data)
    assert len(first.thumbnail_sha256 or "") == 64

    with pytest.raises(ValueError, match="different immutable data"):
        packaging_service.create_packaging_variant(
            publication.id,
            variant_key="hook-a",
            version=1,
            title="Mutated title",
            description="Description",
            thumbnail_storage_key=key,
            created_by="test",
            store=FakeStore(data),
        )


def test_variant_rejects_thumbnail_outside_publication_namespace(packaging_scope) -> None:
    publication = _publication(packaging_scope, suffix="path")
    with pytest.raises(ValueError, match="publication/version packaging namespace"):
        packaging_service.create_packaging_variant(
            publication.id,
            variant_key="hook-a",
            version=1,
            title="Title",
            thumbnail_storage_key="packaging/another/video.png",
            store=FakeStore(b"\x89PNG\r\n\x1a\nbytes"),
        )


def test_activation_is_idempotent_and_cannot_cross_publications(packaging_scope) -> None:
    first_publication = _publication(packaging_scope, suffix="first")
    second_publication = _publication(packaging_scope, suffix="second")
    variant = packaging_service.create_packaging_variant(
        first_publication.id,
        variant_key="baseline",
        version=1,
        title="Baseline",
        store=FakeStore(b"unused"),
    )
    first = packaging_service.register_packaging_activation(
        first_publication.id,
        variant_id=variant.id,
        activation_key="manual-1",
    )
    replay = packaging_service.register_packaging_activation(
        first_publication.id,
        variant_id=variant.id,
        activation_key="manual-1",
    )
    assert replay.id == first.id
    assert first.youtube_video_id == "video-first"

    with pytest.raises(ValueError, match="does not belong"):
        packaging_service.register_packaging_activation(
            second_publication.id,
            variant_id=variant.id,
            activation_key="manual-1",
        )


def test_packaging_routes_are_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/v1/publications/{publication_id}/packaging/variants" in paths
    assert "/v1/publications/{publication_id}/packaging/activations" in paths
