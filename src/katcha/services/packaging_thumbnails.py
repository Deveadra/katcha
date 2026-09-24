from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from katcha.db import session_scope
from katcha.integrations.storage import ObjectStore
from katcha.models import ClipFeature
from katcha.packaging_models import PublicationPackagingVariant
from katcha.production_models import Production
from katcha.publishing_models import Publication
from katcha.rendering.client import render_thumbnail
from katcha.rendering.manifest import ShortBrandSpec
from katcha.rendering.thumbnail_manifest import build_thumbnail_manifest
from katcha.services.packaging import create_packaging_variant
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem


@dataclass(frozen=True, slots=True)
class ThumbnailBuildResult:
    parent_variant: PublicationPackagingVariant
    thumbnail_variant: PublicationPackagingVariant


def _middle_keyframe(features: ClipFeature | None) -> str:
    if features is None or not features.keyframe_keys:
        raise ValueError("thumbnail source clip has no analyzed keyframes")
    keys = [str(value).strip() for value in features.keyframe_keys if str(value).strip()]
    if not keys:
        raise ValueError("thumbnail source clip has no usable keyframes")
    return keys[len(keys) // 2]


def _source_context(
    session: object,
    publication: Publication,
) -> tuple[uuid.UUID, ShortBrandSpec, str, dict[str, object]]:
    if publication.production_id is not None:
        production = session.get(Production, publication.production_id)
        if production is None or production.channel_profile_id is None:
            raise ValueError("publication production has no channel-scoped lineage")
        visual = dict((production.brand_snapshot or {}).get("visual") or {})
        brand = ShortBrandSpec.model_validate(visual)
        frame_key = _middle_keyframe(session.get(ClipFeature, production.clip_id))
        return (
            production.channel_profile_id,
            brand,
            frame_key,
            {
                "source_kind": "production",
                "source_id": str(production.id),
                "clip_id": str(production.clip_id),
                "edit_blueprint_key": production.edit_blueprint_key,
                "edit_blueprint_version": production.edit_blueprint_version,
            },
        )

    if publication.short_episode_id is not None:
        episode = session.get(ShortEpisode, publication.short_episode_id)
        if episode is None:
            raise ValueError("publication short episode lineage is missing")
        payoff = session.scalar(
            select(ShortEpisodeItem).where(
                ShortEpisodeItem.short_episode_id == episode.id,
                ShortEpisodeItem.position == 1,
            )
        )
        if payoff is None:
            raise ValueError("ranked episode has no payoff item for thumbnail grounding")
        visual = dict((episode.brand_snapshot or {}).get("visual") or {})
        brand = ShortBrandSpec.model_validate(visual)
        frame_key = _middle_keyframe(session.get(ClipFeature, payoff.clip_id))
        return (
            episode.channel_profile_id,
            brand,
            frame_key,
            {
                "source_kind": "short_episode",
                "source_id": str(episode.id),
                "clip_id": str(payoff.clip_id),
                "position": payoff.position,
                "role": payoff.role,
                "edit_blueprint_key": episode.edit_blueprint_key,
                "edit_blueprint_version": episode.edit_blueprint_version,
            },
        )

    raise ValueError(
        "grounded thumbnail rendering currently supports production "
        "and short-episode publications"
    )


def _existing_derived_variant(
    session: object,
    *,
    publication_id: uuid.UUID,
    parent_variant_id: uuid.UUID,
) -> PublicationPackagingVariant | None:
    rows = list(
        session.scalars(
            select(PublicationPackagingVariant)
            .where(PublicationPackagingVariant.publication_id == publication_id)
            .order_by(PublicationPackagingVariant.version)
        )
    )
    parent_value = str(parent_variant_id)
    for row in rows:
        metadata = dict(row.variant_metadata or {})
        if (
            metadata.get("thumbnail_parent_variant_id") == parent_value
            and row.thumbnail_storage_key is not None
        ):
            return row
    return None


def build_packaging_thumbnail(
    publication_id: uuid.UUID,
    *,
    parent_variant_id: uuid.UUID,
    store: ObjectStore | None = None,
) -> ThumbnailBuildResult:
    object_store = store or ObjectStore()
    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        parent = session.get(PublicationPackagingVariant, parent_variant_id)
        if parent is None or parent.publication_id != publication_id:
            raise ValueError("parent packaging variant does not belong to publication")
        if parent.thumbnail_storage_key is not None:
            raise ValueError("parent packaging variant already contains a thumbnail")
        brief = dict((parent.variant_metadata or {}).get("thumbnail_brief") or {})
        if not brief:
            raise ValueError("parent packaging variant has no thumbnail creative brief")
        existing = _existing_derived_variant(
            session,
            publication_id=publication_id,
            parent_variant_id=parent_variant_id,
        )
        if existing is not None:
            session.expunge(parent)
            session.expunge(existing)
            return ThumbnailBuildResult(parent, existing)
        profile_id, brand, frame_key, source_metadata = _source_context(
            session,
            publication,
        )
        max_version = int(
            session.scalar(
                select(func.max(PublicationPackagingVariant.version)).where(
                    PublicationPackagingVariant.publication_id == publication_id,
                    PublicationPackagingVariant.variant_key == parent.variant_key,
                )
            )
            or parent.version
        )
        next_version = max_version + 1
        session.expunge(parent)

    if not object_store.exists(frame_key):
        raise ValueError("selected thumbnail keyframe is missing from object storage")

    manifest = build_thumbnail_manifest(
        publication_id=str(publication_id),
        parent_variant_id=str(parent_variant_id),
        variant_key=parent.variant_key,
        variant_version=next_version,
        channel_profile_id=str(profile_id),
        brand=brand,
        source_storage_key=frame_key,
        brief=brief,
    )
    result = render_thumbnail(manifest)
    if not bool((result.metadata or {}).get("verified")):
        raise RuntimeError("thumbnail renderer output failed verification")
    if result.width != 1280 or result.height != 720:
        raise RuntimeError("thumbnail renderer returned invalid dimensions")
    if result.output_key != manifest.output_key:
        raise RuntimeError("thumbnail renderer returned an unexpected object key")

    derived = create_packaging_variant(
        publication_id,
        variant_key=parent.variant_key,
        version=next_version,
        title=parent.title,
        description=parent.description,
        thumbnail_storage_key=result.output_key,
        created_by="katcha-thumbnail-renderer",
        metadata={
            **dict(parent.variant_metadata or {}),
            "thumbnail_parent_variant_id": str(parent_variant_id),
            "thumbnail_source_key": frame_key,
            "thumbnail_source": source_metadata,
            "thumbnail_render_manifest": manifest.model_dump(mode="json"),
            "thumbnail_render_verification": dict(result.metadata or {}),
        },
        store=object_store,
    )
    return ThumbnailBuildResult(parent, derived)
