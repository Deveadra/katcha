from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select

from katcha.clip_lifecycle_models import ClipLifecycle
from katcha.db import session_scope
from katcha.external_edit_models import ExternalEditHandoff
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent
from katcha.models import UsageEvent
from katcha.production_models import Production, ProductionAsset, ProductionScript
from katcha.short_episode_models import (
    ShortEpisode,
    ShortEpisodeAsset,
    ShortEpisodeItem,
    ShortEpisodeScript,
)

SourceType = Literal["production", "short_episode"]


def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    return cleaned.strip("-")[:120] or "asset"


def _clip_media_key(session: Any, clip: Clip) -> str:
    lifecycle = session.get(ClipLifecycle, clip.id)
    if lifecycle is None or lifecycle.lifecycle_state == "hot":
        return clip.storage_key
    if lifecycle.lifecycle_state == "archived" and lifecycle.archive_key:
        return lifecycle.archive_key
    if lifecycle.lifecycle_state == "purged":
        raise ValueError(
            f"clip source media was purged and cannot be handed off: {clip.id}"
        )
    return clip.storage_key


def _object_entry(
    *,
    key: str,
    role: str,
    filename: str,
    content_type: str | None = None,
) -> dict[str, object]:
    return {
        "storage_key": key,
        "role": role,
        "filename": _safe_name(filename),
        "content_type": content_type,
    }


def _production_manifest(
    source_id: uuid.UUID,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    with session_scope() as session:
        production = session.get(Production, source_id)
        if production is None:
            raise ValueError(f"production not found: {source_id}")
        if production.status not in {
            "voiced",
            "rendering",
            "review",
            "approved",
            "failed",
        }:
            raise ValueError(
                "production must reach the voiced stage before external editing handoff"
            )
        clip = session.get(Clip, production.clip_id)
        if clip is None:
            raise ValueError("production source clip is missing")
        script = (
            session.get(ProductionScript, production.selected_script_id)
            if production.selected_script_id
            else None
        )
        assets = list(
            session.scalars(
                select(ProductionAsset)
                .where(
                    ProductionAsset.production_id == source_id,
                    ProductionAsset.kind != "render",
                )
                .order_by(ProductionAsset.kind)
            )
        )
        object_entries = [
            _object_entry(
                key=_clip_media_key(session, clip),
                role="source_video",
                filename=f"source-{clip.id}.{(clip.extension or 'mp4').lstrip('.')}",
                content_type="video/mp4",
            )
        ]
        object_entries.extend(
            _object_entry(
                key=asset.storage_key,
                role=asset.kind,
                filename=f"{asset.kind}-{asset.generation}.{_extension(asset.content_type)}",
                content_type=asset.content_type,
            )
            for asset in assets
        )
        manifest = {
            "schema": "katcha-external-edit-v1",
            "provider": "invideo",
            "source_type": "production",
            "source_id": str(production.id),
            "channel_profile_id": (
                str(production.channel_profile_id)
                if production.channel_profile_id is not None
                else None
            ),
            "persona": {
                "key": production.persona_key,
                "version": production.persona_version,
            },
            "brand": {
                "key": production.brand_key,
                "version": production.brand_version,
                "snapshot": dict(production.brand_snapshot or {}),
            },
            "editing_recipe": {
                "key": production.edit_blueprint_key,
                "version": production.edit_blueprint_version,
                "snapshot": dict(production.edit_blueprint_snapshot or {}),
            },
            "script": (
                {
                    "narration": script.narration,
                    "interaction_prompt": script.interaction_prompt,
                    "style": script.style,
                    "metadata": dict(script.script_metadata or {}),
                }
                if script is not None
                else None
            ),
            "instructions": _invideo_instructions(
                brand=dict(production.brand_snapshot or {}),
                blueprint=dict(production.edit_blueprint_snapshot or {}),
            ),
            "assets": object_entries,
        }
        return manifest, object_entries


def _short_episode_manifest(
    source_id: uuid.UUID,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    with session_scope() as session:
        episode = session.get(ShortEpisode, source_id)
        if episode is None:
            raise ValueError(f"short episode not found: {source_id}")
        if episode.status not in {
            "voiced",
            "editorial_approved",
            "rendering",
            "rendered",
            "render_review",
            "approved",
            "failed",
            "render_failed",
        }:
            raise ValueError(
                "short episode must reach the voiced stage before external editing handoff"
            )
        items = list(
            session.scalars(
                select(ShortEpisodeItem)
                .where(ShortEpisodeItem.short_episode_id == source_id)
                .order_by(ShortEpisodeItem.position)
            )
        )
        clips = {
            clip.id: clip
            for clip in session.scalars(
                select(Clip).where(Clip.id.in_([item.clip_id for item in items]))
            )
        }
        script = (
            session.get(ShortEpisodeScript, episode.selected_script_id)
            if episode.selected_script_id
            else None
        )
        assets = list(
            session.scalars(
                select(ShortEpisodeAsset)
                .where(
                    ShortEpisodeAsset.short_episode_id == source_id,
                    ShortEpisodeAsset.kind != "render",
                )
                .order_by(ShortEpisodeAsset.kind)
            )
        )
        object_entries: list[dict[str, object]] = []
        item_payloads: list[dict[str, object]] = []
        for item in items:
            clip = clips.get(item.clip_id)
            if clip is None:
                raise ValueError(f"short episode clip is missing: {item.clip_id}")
            filename = (
                f"{item.position:02d}-{item.role}-{clip.id}."
                f"{(clip.extension or 'mp4').lstrip('.')}"
            )
            object_entries.append(
                _object_entry(
                    key=_clip_media_key(session, clip),
                    role=f"source_video_{item.position:02d}",
                    filename=filename,
                    content_type="video/mp4",
                )
            )
            item_payloads.append(
                {
                    "position": item.position,
                    "clip_id": str(item.clip_id),
                    "role": item.role,
                    "editorial_signals": dict(item.editorial_signals or {}),
                    "source_snapshot": list(item.source_snapshot or []),
                }
            )
        object_entries.extend(
            _object_entry(
                key=asset.storage_key,
                role=asset.kind,
                filename=f"{asset.kind}-{asset.generation}.{_extension(asset.content_type)}",
                content_type=asset.content_type,
            )
            for asset in assets
        )
        manifest = {
            "schema": "katcha-external-edit-v1",
            "provider": "invideo",
            "source_type": "short_episode",
            "source_id": str(episode.id),
            "channel_profile_id": str(episode.channel_profile_id),
            "premise": episode.premise,
            "format": {
                "key": episode.format_key,
                "version": episode.format_version,
                "snapshot": dict(episode.format_snapshot or {}),
            },
            "persona": {
                "key": episode.persona_key,
                "version": episode.persona_version,
            },
            "brand": {
                "key": episode.brand_key,
                "version": episode.brand_version,
                "snapshot": dict(episode.brand_snapshot or {}),
            },
            "editing_recipe": {
                "key": episode.edit_blueprint_key,
                "version": episode.edit_blueprint_version,
                "snapshot": dict(episode.edit_blueprint_snapshot or {}),
            },
            "items": item_payloads,
            "script": (
                {
                    "payload": dict(script.script_payload or {}),
                    "narration_beats": list(script.narration_beats or []),
                    "style": script.style,
                }
                if script is not None
                else None
            ),
            "instructions": _invideo_instructions(
                brand=dict(episode.brand_snapshot or {}),
                blueprint=dict(episode.edit_blueprint_snapshot or {}),
            ),
            "assets": object_entries,
        }
        return manifest, object_entries


def _extension(content_type: str | None) -> str:
    return {
        "audio/wav": "wav",
        "audio/mpeg": "mp3",
        "application/json": "json",
        "image/png": "png",
        "image/webp": "webp",
        "video/mp4": "mp4",
    }.get(str(content_type or "").lower(), "bin")


def _invideo_instructions(
    *,
    brand: dict[str, object],
    blueprint: dict[str, object],
) -> list[str]:
    instructions = [
        "Treat the supplied Katcha assets and script as the source of truth.",
        "Do not replace supplied source footage with generated footage unless explicitly asked.",
        "Preserve source ordering and narration meaning.",
        "Export one final MP4 suitable for Katcha review; do not publish directly.",
    ]
    if brand:
        instructions.append("Follow brand.json exactly for logo, typography, colors, and CTA.")
    if blueprint:
        instructions.append(
            "Follow editing-recipe.json for framing, captions, audio policy, and pacing."
        )
    return instructions


def prepare_invideo_handoff(
    source_type: SourceType,
    source_id: uuid.UUID,
    *,
    actor: str = "operator",
    note: str | None = None,
) -> ExternalEditHandoff:
    if source_type == "production":
        manifest, assets = _production_manifest(source_id)
    elif source_type == "short_episode":
        manifest, assets = _short_episode_manifest(source_id)
    else:
        raise ValueError(f"unsupported external edit source type: {source_type}")

    store = ObjectStore()
    store.ensure_bucket()
    for asset in assets:
        key = str(asset["storage_key"])
        if not store.exists(key):
            raise ValueError(f"external edit asset is missing from storage: {key}")

    with session_scope() as session:
        generation = int(
            session.scalar(
                select(func.coalesce(func.max(ExternalEditHandoff.generation), 0)).where(
                    ExternalEditHandoff.provider == "invideo",
                    ExternalEditHandoff.source_type == source_type,
                    ExternalEditHandoff.source_id == source_id,
                )
            )
            or 0
        ) + 1
        handoff = ExternalEditHandoff(
            provider="invideo",
            source_type=source_type,
            source_id=source_id,
            generation=generation,
            status="prepared",
            package_manifest_key="pending",
            handoff_metadata={
                "actor": actor,
                "note": note,
                "transport": "manual_bridge",
                "api_status": "awaiting_documented_invideo_api_contract",
            },
        )
        session.add(handoff)
        session.flush()
        manifest["handoff_id"] = str(handoff.id)
        manifest["generation"] = generation
        key = f"external-edit/invideo/{handoff.id}/manifest.json"
        store.put_bytes(
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            key,
            content_type="application/json",
        )
        handoff.package_manifest_key = key
        session.add(
            DomainEvent(
                aggregate_type=source_type,
                aggregate_id=str(source_id),
                event_type="external_edit.invideo_prepared",
                payload={
                    "handoff_id": str(handoff.id),
                    "provider": "invideo",
                    "generation": generation,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(handoff)
        session.expunge(handoff)
        return handoff


def handoff_manifest(handoff_id: uuid.UUID) -> dict[str, object]:
    with session_scope() as session:
        row = session.get(ExternalEditHandoff, handoff_id)
        if row is None:
            raise ValueError("external edit handoff not found")
        key = row.package_manifest_key
    return json.loads(ObjectStore().get_bytes(key).decode("utf-8"))


def build_handoff_zip(handoff_id: uuid.UUID) -> Path:
    manifest = handoff_manifest(handoff_id)
    work_dir = Path(tempfile.mkdtemp(prefix=f"katcha-invideo-{handoff_id}-"))
    archive = work_dir / f"katcha-invideo-{handoff_id}.zip"
    store = ObjectStore()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
        handle.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True),
        )
        handle.writestr(
            "README.txt",
            "\n".join(
                [
                    "Katcha → InVideo external edit package",
                    "",
                    "Upload the files in assets/ to InVideo.",
                    "Use script.json, brand.json, editing-recipe.json, and manifest.json",
                    "as the authoritative production brief.",
                    "Return one final MP4 to Katcha; do not publish from InVideo.",
                    "",
                    *[f"- {line}" for line in manifest.get("instructions", [])],
                ]
            ),
        )
        brand = dict(manifest.get("brand") or {})
        blueprint = dict(manifest.get("editing_recipe") or {})
        script = manifest.get("script")
        handle.writestr(
            "brand.json",
            json.dumps(brand.get("snapshot") or {}, indent=2, sort_keys=True),
        )
        handle.writestr(
            "editing-recipe.json",
            json.dumps(blueprint.get("snapshot") or {}, indent=2, sort_keys=True),
        )
        handle.writestr(
            "script.json",
            json.dumps(script or {}, indent=2, sort_keys=True),
        )
        for asset in manifest.get("assets", []):
            if not isinstance(asset, dict):
                continue
            key = str(asset.get("storage_key") or "")
            filename = str(asset.get("filename") or "asset.bin")
            if not key:
                continue
            local = work_dir / filename
            store.download_file(key, local)
            handle.write(local, arcname=f"assets/{filename}")
            local.unlink(missing_ok=True)
    return archive


def import_external_output(
    handoff_id: uuid.UUID,
    path: Path,
    *,
    external_project_id: str | None = None,
    actor: str = "operator",
) -> ExternalEditHandoff:
    verification = _probe_video(path)
    with session_scope() as session:
        row = session.get(ExternalEditHandoff, handoff_id)
        if row is None:
            raise ValueError("external edit handoff not found")
        if row.status in {"adopted", "cancelled"}:
            raise ValueError(f"cannot import output for {row.status} handoff")
        output_key = f"external-edit/invideo/{handoff_id}/output.mp4"
        source_type = row.source_type
        source_id = row.source_id

    store = ObjectStore()
    store.ensure_bucket()
    store.put_file(path, output_key, content_type="video/mp4")

    with session_scope() as session:
        row = session.get(ExternalEditHandoff, handoff_id)
        if row is None:
            raise RuntimeError("external edit handoff disappeared")
        row.status = "output_imported"
        row.output_key = output_key
        row.external_project_id = external_project_id
        row.completed_at = datetime.now(UTC)
        row.handoff_metadata = {
            **dict(row.handoff_metadata or {}),
            "verification": verification,
            "output_imported_by": actor,
        }
        session.add(
            DomainEvent(
                aggregate_type=source_type,
                aggregate_id=str(source_id),
                event_type="external_edit.invideo_output_imported",
                payload={
                    "handoff_id": str(handoff_id),
                    "output_key": output_key,
                    "external_project_id": external_project_id,
                    "verification": verification,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def record_external_edit_metrics(
    handoff_id: uuid.UUID,
    *,
    actor: str = "operator",
    credits_used: float | None = None,
    cost_usd: float | None = None,
    production_minutes: float | None = None,
    manual_interventions: int | None = None,
    notes: str | None = None,
) -> ExternalEditHandoff:
    """Record provider spend and production-efficiency evidence for a handoff.

    Metrics are deliberately attached to the immutable handoff rather than the
    current episode. That keeps provider comparisons version-specific and lets
    later analytics correlate them with the resulting YouTube performance.
    """
    values = {
        "credits_used": credits_used,
        "cost_usd": cost_usd,
        "production_minutes": production_minutes,
        "manual_interventions": manual_interventions,
        "notes": notes,
    }
    if credits_used is not None and credits_used < 0:
        raise ValueError("credits_used must be non-negative")
    if cost_usd is not None and cost_usd < 0:
        raise ValueError("cost_usd must be non-negative")
    if production_minutes is not None and production_minutes < 0:
        raise ValueError("production_minutes must be non-negative")
    if manual_interventions is not None and manual_interventions < 0:
        raise ValueError("manual_interventions must be non-negative")

    with session_scope() as session:
        row = session.get(ExternalEditHandoff, handoff_id)
        if row is None:
            raise ValueError("external edit handoff not found")
        if row.status == "cancelled":
            raise ValueError("cannot record metrics for a cancelled handoff")
        metadata = dict(row.handoff_metadata or {})
        existing = dict(metadata.get("provider_metrics") or {})
        existing.update({key: value for key, value in values.items() if value is not None})
        existing["recorded_by"] = actor
        existing["recorded_at"] = datetime.now(UTC).isoformat()
        metadata["provider_metrics"] = existing
        row.handoff_metadata = metadata

        if cost_usd is not None:
            session.add(
                UsageEvent(
                    task="external_edit",
                    provider=row.provider,
                    model="manual_bridge",
                    cost_usd=cost_usd,
                    reference_type=row.source_type,
                    reference_id=str(row.source_id),
                    usage_metadata={
                        "handoff_id": str(row.id),
                        "credits_used": credits_used,
                        "production_minutes": production_minutes,
                        "manual_interventions": manual_interventions,
                    },
                )
            )
        session.add(
            DomainEvent(
                aggregate_type=row.source_type,
                aggregate_id=str(row.source_id),
                event_type="external_edit.invideo_metrics_recorded",
                payload={"handoff_id": str(row.id), "provider_metrics": existing},
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def _next_production_render_generation(
    session: Any,
    production_id: uuid.UUID,
) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.max(ProductionAsset.generation), 0)).where(
                ProductionAsset.production_id == production_id,
                ProductionAsset.kind == "render",
            )
        )
        or 0
    ) + 1


def _next_short_episode_render_generation(
    session: Any,
    short_episode_id: uuid.UUID,
) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.max(ShortEpisodeAsset.generation), 0)).where(
                ShortEpisodeAsset.short_episode_id == short_episode_id,
                ShortEpisodeAsset.kind == "render",
            )
        )
        or 0
    ) + 1


def adopt_external_output(
    handoff_id: uuid.UUID,
    *,
    actor: str = "operator",
) -> ExternalEditHandoff:
    with session_scope() as session:
        row = session.get(ExternalEditHandoff, handoff_id)
        if row is None:
            raise ValueError("external edit handoff not found")
        if row.status != "output_imported" or not row.output_key:
            raise ValueError("external edit output must be imported before adoption")
        verification = dict((row.handoff_metadata or {}).get("verification") or {})
        if not verification.get("verified"):
            raise ValueError("external edit output is not media-verified")

        if row.source_type == "production":
            source = session.get(Production, row.source_id)
            if source is None:
                raise ValueError("production not found")
            render_generation = _next_production_render_generation(
                session,
                source.id,
            )
            session.add(
                ProductionAsset(
                    production_id=source.id,
                    kind="render",
                    generation=render_generation,
                    storage_key=row.output_key,
                    content_type="video/mp4",
                    provider="invideo",
                    model="manual_bridge",
                    asset_metadata={
                        "verified": True,
                        "external_edit_handoff_id": str(row.id),
                        "external_edit_generation": row.generation,
                        **verification,
                    },
                )
            )
            source.status = "review"
            source.stage = "render_verified"
            source.error = None
        else:
            source = session.get(ShortEpisode, row.source_id)
            if source is None:
                raise ValueError("short episode not found")
            render_generation = _next_short_episode_render_generation(
                session,
                source.id,
            )
            session.add(
                ShortEpisodeAsset(
                    short_episode_id=source.id,
                    kind="render",
                    generation=render_generation,
                    storage_key=row.output_key,
                    content_type="video/mp4",
                    provider="invideo",
                    model="manual_bridge",
                    asset_metadata={
                        "verified": True,
                        "external_edit_handoff_id": str(row.id),
                        "external_edit_generation": row.generation,
                        **verification,
                    },
                )
            )
            source.status = "render_review"
            source.stage = "render_review"
            source.error = None

        row.status = "adopted"
        row.handoff_metadata = {
            **dict(row.handoff_metadata or {}),
            "adopted_by": actor,
            "adopted_at": datetime.now(UTC).isoformat(),
            "adopted_render_generation": render_generation,
        }
        session.add(
            DomainEvent(
                aggregate_type=row.source_type,
                aggregate_id=str(row.source_id),
                event_type="external_edit.invideo_adopted",
                payload={
                    "handoff_id": str(row.id),
                    "provider": "invideo",
                    "output_key": row.output_key,
                    "render_generation": render_generation,
                    "actor": actor,
                },
            )
        )
        session.flush()
        session.refresh(row)
        session.expunge(row)
        return row


def _probe_video(path: Path) -> dict[str, object]:
    process = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if process.returncode != 0:
        raise ValueError(
            f"external edit output failed ffprobe: {process.stderr.strip()[:500]}"
        )
    payload = json.loads(process.stdout or "{}")
    duration = float((payload.get("format") or {}).get("duration") or 0)
    streams = list(payload.get("streams") or [])
    video = next(
        (item for item in streams if item.get("codec_type") == "video"),
        None,
    )
    if duration <= 0 or not isinstance(video, dict):
        raise ValueError("external edit output is not a valid non-empty video")
    return {
        "verified": True,
        "duration_seconds": round(duration, 3),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "size_bytes": path.stat().st_size,
    }
