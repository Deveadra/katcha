from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from katcha.api.main import app
from katcha.branding import channel_01_brand_v1
from katcha.db import Base
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, ClipFeature
from katcha.packaging_models import PublicationPackagingVariant
from katcha.production_models import Production
from katcha.publishing_models import Publication, YouTubeConnection
from katcha.rendering.client import ThumbnailRenderResult
from katcha.rendering.manifest import ShortBrandSpec
from katcha.rendering.thumbnail_manifest import build_thumbnail_manifest
from katcha.services import packaging as packaging_service
from katcha.services import packaging_thumbnails
from katcha.services.packaging import create_packaging_variant


class FakeStore:
    data = b"\x89PNG\r\n\x1a\nthumbnail-bytes"

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
def thumbnail_scope(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session: Session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(packaging_service, "session_scope", scope)
    monkeypatch.setattr(packaging_thumbnails, "session_scope", scope)
    return scope


def _setup_publication(scope) -> tuple[Publication, PublicationPackagingVariant]:
    brand = channel_01_brand_v1()
    with scope() as session:
        connection = YouTubeConnection(
            channel_id="thumbnail-channel",
            channel_title="Thumbnail Channel",
            status="active",
            scopes=["youtube"],
            encrypted_access_token="encrypted",
            encrypted_refresh_token="encrypted",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(connection)
        session.flush()
        profile = ChannelProfile(
            youtube_connection_id=connection.id,
            timezone="UTC",
        )
        session.add(profile)
        session.flush()
        clip = Clip(
            sha256="a" * 64,
            storage_key="raw/source.mp4",
            duration_seconds=10,
            width=1080,
            height=1920,
        )
        session.add(clip)
        session.flush()
        session.add(
            ClipFeature(
                clip_id=clip.id,
                keyframe_keys=[
                    "analysis/a/frame-001.jpg",
                    "analysis/a/frame-002.jpg",
                    "analysis/a/frame-003.jpg",
                ],
            )
        )
        production = Production(
            clip_id=clip.id,
            channel_profile_id=profile.id,
            workflow_id="thumbnail-production",
            persona_key="youth_host",
            persona_version="2.0.0",
            prompt_version="short-script-v1",
            brand_key=brand.brand_key,
            brand_version=brand.version,
            brand_snapshot=brand.model_dump(mode="json"),
            edit_blueprint_key="persona_commentary",
            edit_blueprint_version=1,
        )
        session.add(production)
        session.flush()
        publication = Publication(
            production_id=production.id,
            youtube_connection_id=connection.id,
            workflow_id="thumbnail-publication",
            analytics_workflow_id="thumbnail-analytics",
            title="Original title",
            description="Original description",
            youtube_video_id="thumbnail-video",
        )
        session.add(publication)
        session.flush()
        publication_id = publication.id

    parent = create_packaging_variant(
        publication_id,
        variant_key="ai-candidate",
        version=1,
        title="A grounded title",
        description="Grounded description",
        created_by="katcha-ai",
        metadata={
            "thumbnail_brief": {
                "concept": "Use the real jump frame",
                "focal_subject": "The visible subject",
                "composition": "Tight crop",
                "on_image_text": "NO WAY",
                "emotion": "surprise",
                "avoid": [],
            }
        },
        store=FakeStore(),
    )
    with scope() as session:
        publication = session.get(Publication, publication_id)
        session.expunge(publication)
    return publication, parent


def test_thumbnail_manifest_freezes_variant_namespace_and_brand() -> None:
    brand = ShortBrandSpec()
    manifest = build_thumbnail_manifest(
        publication_id="publication-1",
        parent_variant_id="variant-1",
        variant_key="hook-a",
        variant_version=2,
        channel_profile_id="channel-1",
        brand=brand,
        source_storage_key="analysis/frame.jpg",
        brief={"on_image_text": "Wait for it"},
    )

    assert manifest.width == 1280
    assert manifest.height == 720
    assert manifest.output_key == "packaging/publication-1/hook-a/v2/thumbnail.png"
    assert manifest.brand_key == brand.brand_key


def test_thumbnail_build_creates_one_immutable_derived_version(
    thumbnail_scope,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication, parent = _setup_publication(thumbnail_scope)
    calls = {"count": 0}

    def fake_render(manifest):
        calls["count"] += 1
        return ThumbnailRenderResult(
            output_key=manifest.output_key,
            width=1280,
            height=720,
            metadata={
                "verified": True,
                "verification_mode": "png+ffprobe+object-head",
            },
        )

    monkeypatch.setattr(packaging_thumbnails, "render_thumbnail", fake_render)
    first = packaging_thumbnails.build_packaging_thumbnail(
        publication.id,
        parent_variant_id=parent.id,
        store=FakeStore(),
    )
    second = packaging_thumbnails.build_packaging_thumbnail(
        publication.id,
        parent_variant_id=parent.id,
        store=FakeStore(),
    )

    assert calls["count"] == 1
    assert first.thumbnail_variant.version == 2
    assert second.thumbnail_variant.id == first.thumbnail_variant.id
    assert first.thumbnail_variant.thumbnail_content_type == "image/png"
    assert len(first.thumbnail_variant.thumbnail_sha256 or "") == 64
    assert (
        first.thumbnail_variant.variant_metadata["thumbnail_parent_variant_id"]
        == str(parent.id)
    )
    assert (
        first.thumbnail_variant.variant_metadata["thumbnail_source_key"]
        == "analysis/a/frame-002.jpg"
    )


def test_thumbnail_route_is_mounted() -> None:
    assert (
        "/v1/publications/{publication_id}/packaging/thumbnails"
        in set(app.openapi()["paths"])
    )
