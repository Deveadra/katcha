from __future__ import annotations

import uuid

from temporalio import activity

from katcha.brand_preview_models import BrandPreviewRender
from katcha.db import session_scope
from katcha.models import DomainEvent
from katcha.rendering.client import render_short
from katcha.rendering.manifest import ShortRenderManifest


@activity.defn
def render_brand_preview_activity(preview_id: str) -> dict[str, object]:
    preview_uuid = uuid.UUID(preview_id)
    with session_scope() as session:
        row = session.get(BrandPreviewRender, preview_uuid)
        if row is None:
            raise ValueError(f"brand preview not found: {preview_id}")
        if row.status == "verified" and row.output_key:
            return {
                "brand_preview_id": preview_id,
                "output_key": row.output_key,
                "reused": True,
                "verified": True,
            }
        manifest = ShortRenderManifest.model_validate(row.render_manifest)
        if not manifest.output_key.startswith("previews/brands/"):
            raise RuntimeError("brand preview output is outside preview namespace")
        row.status = "rendering"
        row.error = None

    result = render_short(manifest)
    if not bool((result.metadata or {}).get("verified")):
        raise RuntimeError("brand preview renderer output failed verification")

    verification = {
        **dict(result.metadata or {}),
        "duration_seconds": result.duration_seconds,
        "output_key": result.output_key,
    }
    with session_scope() as session:
        row = session.get(BrandPreviewRender, preview_uuid)
        if row is None:
            raise RuntimeError("brand preview disappeared after render")
        row.status = "verified"
        row.output_key = result.output_key
        row.verification = verification
        row.error = None
        session.add(
            DomainEvent(
                aggregate_type="brand_preview",
                aggregate_id=str(row.id),
                event_type="brand_preview.verified",
                payload={
                    "brand_preview_id": str(row.id),
                    "channel_profile_id": str(row.channel_profile_id),
                    "production_id": str(row.production_id),
                    "brand_key": row.brand_key,
                    "brand_version": row.brand_version,
                    "output_key": result.output_key,
                    "verification": verification,
                },
            )
        )
    return {
        "brand_preview_id": preview_id,
        "output_key": result.output_key,
        "reused": False,
        "verified": True,
    }


@activity.defn
def mark_brand_preview_failed_activity(preview_id: str, message: str) -> None:
    from katcha.services.brand_previews import mark_brand_preview_failed

    mark_brand_preview_failed(uuid.UUID(preview_id), message)
