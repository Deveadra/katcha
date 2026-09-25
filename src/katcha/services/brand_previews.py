from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from sqlalchemy import select

from katcha.brand_models import ChannelBrandVersion
from katcha.brand_preview_models import BrandPreviewRender
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.production_models import Production
from katcha.rendering.manifest import ShortBrandSpec, ShortRenderManifest
from katcha.rendering.ranked_episode_manifest import RankedEpisodeRenderManifest
from katcha.rendering.reactions import ReactionAssetPack, ReactionCue, resolve_reaction_events
from katcha.services.channel_brands import brand_contract
from katcha.services.channel_profiles import ensure_active_profile
from katcha.short_episode_models import ShortEpisode

PreviewSourceKind = Literal["production", "short_episode"]


def _request_key(
    source_kind: PreviewSourceKind,
    source_id: uuid.UUID,
    brand_version: int,
    reaction_cue: dict[str, Any] | None,
) -> str:
    payload = {
        "source_kind": source_kind,
        "source_id": str(source_id),
        "brand_version": brand_version,
        "reaction_cue": reaction_cue,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preview_brand(
    contract_visual: dict[str, Any],
    *,
    brand_key: str,
    brand_version: int,
) -> ShortBrandSpec:
    preview_brand = ShortBrandSpec.model_validate(contract_visual)
    if preview_brand.brand_key != brand_key or preview_brand.version != brand_version:
        raise ValueError("staged brand visual identity does not match brand contract")
    return preview_brand


def _reaction_events(
    *,
    contract_visual: dict[str, Any],
    preview_brand: ShortBrandSpec,
    reaction_cue: dict[str, Any] | None,
    narration_windows: dict[int, tuple[float, float]],
    output_duration_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if reaction_cue is None:
        return [], None
    cue = ReactionCue.model_validate(reaction_cue)
    pack_payload = contract_visual.get("reaction_pack")
    if not isinstance(pack_payload, dict):
        raise ValueError("staged brand does not define a reaction asset pack")
    pack = ReactionAssetPack.model_validate(pack_payload)
    events = resolve_reaction_events(
        cues=[cue],
        pack=pack,
        brand_key=preview_brand.brand_key,
        narration_windows=narration_windows,
        output_duration_seconds=output_duration_seconds,
    )
    return (
        [event.model_dump(mode="json") for event in events],
        cue.model_dump(mode="json"),
    )


def compile_brand_preview_manifest(
    base: ShortRenderManifest,
    *,
    preview_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    contract_visual: dict[str, Any],
    brand_key: str,
    brand_version: int,
    production_id: uuid.UUID,
    reaction_cue: dict[str, Any] | None = None,
) -> tuple[ShortRenderManifest, dict[str, Any] | None]:
    preview_brand = _preview_brand(
        contract_visual,
        brand_key=brand_key,
        brand_version=brand_version,
    )
    reaction_events, normalized_cue = _reaction_events(
        contract_visual=contract_visual,
        preview_brand=preview_brand,
        reaction_cue=reaction_cue,
        narration_windows={
            index: (overlay.start_seconds, overlay.duration_seconds)
            for index, overlay in enumerate(base.overlays)
        },
        output_duration_seconds=base.output_duration_seconds,
    )
    output_key = (
        f"previews/brands/{channel_profile_id}/v{brand_version}/"
        f"production-{production_id}/{preview_id}.mp4"
    )
    preview = ShortRenderManifest.model_validate(
        {
            **base.model_dump(mode="json"),
            "production_id": f"brand-preview-{preview_id}",
            "output_key": output_key,
            "brand": preview_brand.model_dump(mode="json"),
            "reaction_events": reaction_events,
        }
    )
    return preview, normalized_cue


def compile_ranked_brand_preview_manifest(
    base: RankedEpisodeRenderManifest,
    *,
    preview_id: uuid.UUID,
    channel_profile_id: uuid.UUID,
    contract_visual: dict[str, Any],
    brand_key: str,
    brand_version: int,
    short_episode_id: uuid.UUID,
    reaction_cue: dict[str, Any] | None = None,
) -> tuple[RankedEpisodeRenderManifest, dict[str, Any] | None]:
    preview_brand = _preview_brand(
        contract_visual,
        brand_key=brand_key,
        brand_version=brand_version,
    )
    reaction_events, normalized_cue = _reaction_events(
        contract_visual=contract_visual,
        preview_brand=preview_brand,
        reaction_cue=reaction_cue,
        narration_windows={
            overlay.sequence: (overlay.start_seconds, overlay.duration_seconds)
            for overlay in base.overlays
        },
        output_duration_seconds=base.output_duration_seconds,
    )
    treatment = base.treatment.model_dump(mode="json")
    treatment["brand_key"] = brand_key
    treatment["brand_version"] = brand_version
    output_key = (
        f"previews/brands/{channel_profile_id}/v{brand_version}/"
        f"short-episode-{short_episode_id}/{preview_id}.mp4"
    )
    preview = RankedEpisodeRenderManifest.model_validate(
        {
            **base.model_dump(mode="json"),
            "short_episode_id": f"brand-preview-{preview_id}",
            "output_key": output_key,
            "brand": preview_brand.model_dump(mode="json"),
            "treatment": treatment,
            "reaction_events": reaction_events,
        }
    )
    return preview, normalized_cue


def _retry_existing(
    session: object,
    existing: BrandPreviewRender,
) -> BrandPreviewRender:
    if existing.status == "failed":
        existing.workflow_attempt += 1
        existing.workflow_id = f"brand-preview-{existing.id}-a{existing.workflow_attempt}"
        existing.status = "queued"
        existing.error = None
        existing.verification = {}
        session.add(
            DomainEvent(
                aggregate_type="brand_preview",
                aggregate_id=str(existing.id),
                event_type="brand_preview.retry_queued",
                payload={
                    "brand_preview_id": str(existing.id),
                    "workflow_attempt": existing.workflow_attempt,
                    "workflow_id": existing.workflow_id,
                },
            )
        )
        session.flush()
        session.refresh(existing)
    session.expunge(existing)
    return existing


def register_brand_preview(
    channel_profile_id: uuid.UUID,
    *,
    brand_version: int,
    production_id: uuid.UUID | None = None,
    short_episode_id: uuid.UUID | None = None,
    reaction_cue: dict[str, Any] | None = None,
) -> BrandPreviewRender:
    if (production_id is None) == (short_episode_id is None):
        raise ValueError("brand preview requires exactly one source")

    with session_scope() as session:
        profile = ensure_active_profile(session, channel_profile_id)
        target = session.scalar(
            select(ChannelBrandVersion).where(
                ChannelBrandVersion.channel_profile_id == profile.id,
                ChannelBrandVersion.version == brand_version,
            )
        )
        if target is None:
            raise ValueError(f"brand version not found for channel: {brand_version}")
        if target.is_active:
            raise ValueError("brand preview requires a staged inactive brand version")
        contract = brand_contract(target)
        visual = dict(contract.visual or {})
        normalized_request_cue = (
            ReactionCue.model_validate(reaction_cue).model_dump(mode="json")
            if reaction_cue is not None
            else None
        )

        source_kind: PreviewSourceKind
        source_id: uuid.UUID
        source_brand_key: str | None
        source_brand_version: int | None
        edit_blueprint_key: str | None
        edit_blueprint_version: int | None
        base_version: str

        if production_id is not None:
            source_kind = "production"
            source_id = production_id
            source = session.get(Production, production_id)
            if source is None:
                raise ValueError(f"production not found: {production_id}")
            if source.channel_profile_id != profile.id:
                raise ValueError("production belongs to a different channel")
            if not source.render_manifest:
                raise ValueError("production has no frozen render manifest")
            base_payload = dict(source.render_manifest)
            if base_payload.get("version") != "short-render-v1":
                raise ValueError("production preview requires short-render-v1")
            base = ShortRenderManifest.model_validate(base_payload)
            source_brand_key = source.brand_key
            source_brand_version = source.brand_version
            edit_blueprint_key = source.edit_blueprint_key
            edit_blueprint_version = source.edit_blueprint_version
            base_version = base.version
        else:
            source_kind = "short_episode"
            source_id = short_episode_id
            source = session.get(ShortEpisode, short_episode_id)
            if source is None:
                raise ValueError(f"short episode not found: {short_episode_id}")
            if source.channel_profile_id != profile.id:
                raise ValueError("short episode belongs to a different channel")
            if not source.render_manifest:
                raise ValueError("short episode has no frozen render manifest")
            base_payload = dict(source.render_manifest)
            if base_payload.get("version") != "ranked-episode-render-v1":
                raise ValueError("short episode preview requires ranked-episode-render-v1")
            base = RankedEpisodeRenderManifest.model_validate(base_payload)
            source_brand_key = source.brand_key
            source_brand_version = source.brand_version
            edit_blueprint_key = source.edit_blueprint_key
            edit_blueprint_version = source.edit_blueprint_version
            base_version = base.version

        request_key = _request_key(
            source_kind,
            source_id,
            brand_version,
            normalized_request_cue,
        )
        existing = session.scalar(
            select(BrandPreviewRender).where(
                BrandPreviewRender.channel_profile_id == profile.id,
                BrandPreviewRender.request_key == request_key,
            )
        )
        if existing is not None:
            return _retry_existing(session, existing)

        preview_id = uuid.uuid4()
        if source_kind == "production":
            preview_manifest, normalized_cue = compile_brand_preview_manifest(
                base,
                preview_id=preview_id,
                channel_profile_id=profile.id,
                contract_visual=visual,
                brand_key=contract.brand_key,
                brand_version=contract.version,
                production_id=source_id,
                reaction_cue=normalized_request_cue,
            )
        else:
            preview_manifest, normalized_cue = compile_ranked_brand_preview_manifest(
                base,
                preview_id=preview_id,
                channel_profile_id=profile.id,
                contract_visual=visual,
                brand_key=contract.brand_key,
                brand_version=contract.version,
                short_episode_id=source_id,
                reaction_cue=normalized_request_cue,
            )

        row = BrandPreviewRender(
            id=preview_id,
            channel_profile_id=profile.id,
            production_id=source_id if source_kind == "production" else None,
            short_episode_id=source_id if source_kind == "short_episode" else None,
            brand_version_id=target.id,
            brand_key=contract.brand_key,
            brand_version=target.version,
            request_key=request_key,
            workflow_id=f"brand-preview-{preview_id}-a1",
            workflow_attempt=1,
            status="queued",
            source_lineage={
                "source_kind": source_kind,
                "source_id": str(source_id),
                "source_brand_key": source_brand_key,
                "source_brand_version": source_brand_version,
                "edit_blueprint_key": edit_blueprint_key,
                "edit_blueprint_version": edit_blueprint_version,
                "source_render_manifest_version": base_version,
            },
            brand_snapshot=contract.model_dump(mode="json"),
            reaction_cue=normalized_cue,
            render_manifest=preview_manifest.model_dump(mode="json"),
            output_key=None,
            verification={},
            error=None,
        )
        session.add(row)
        session.flush()
        session.add(
            DomainEvent(
                aggregate_type="brand_preview",
                aggregate_id=str(row.id),
                event_type="brand_preview.registered",
                payload={
                    "brand_preview_id": str(row.id),
                    "channel_profile_id": str(profile.id),
                    "source_kind": source_kind,
                    "source_id": str(source_id),
                    "brand_key": contract.brand_key,
                    "brand_version": contract.version,
                    "output_key": preview_manifest.output_key,
                    "reaction_cue": normalized_cue,
                },
            )
        )
        session.refresh(row)
        session.expunge(row)
        return row


def get_brand_preview(
    channel_profile_id: uuid.UUID,
    preview_id: uuid.UUID,
) -> BrandPreviewRender:
    with session_scope() as session:
        ensure_active_profile(session, channel_profile_id)
        row = session.get(BrandPreviewRender, preview_id)
        if row is None or row.channel_profile_id != channel_profile_id:
            raise ValueError(f"brand preview not found: {preview_id}")
        session.expunge(row)
        return row


def mark_brand_preview_failed(preview_id: uuid.UUID, message: str) -> None:
    with session_scope() as session:
        row = session.get(BrandPreviewRender, preview_id)
        if row is None:
            return
        row.status = "failed"
        row.error = message[:8000]
        session.add(
            DomainEvent(
                aggregate_type="brand_preview",
                aggregate_id=str(row.id),
                event_type="brand_preview.failed",
                payload={"brand_preview_id": str(row.id), "error": row.error},
            )
        )
