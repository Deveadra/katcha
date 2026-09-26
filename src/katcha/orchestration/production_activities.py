from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from temporalio import activity

from katcha.audio.tts import VoiceProfile, get_voice_profile, synthesize_speech
from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.domain import ProductionStatus
from katcha.editing.blueprints import EditBlueprintContract
from katcha.editorial.generator import generate_short_scripts
from katcha.editorial.personas import get_persona
from katcha.integrations.storage import ObjectStore
from katcha.models import Clip, DomainEvent, UsageEvent
from katcha.production_models import Production, ProductionAsset, ProductionScript
from katcha.rendering.blueprint_manifest import (
    BlueprintRenderManifest,
    build_blueprint_render_manifest,
)
from katcha.rendering.client import render_blueprint, render_short
from katcha.rendering.manifest import (
    ShortBrandSpec,
    ShortRenderManifest,
    build_short_manifest,
)
from katcha.rendering.reactions import ReactionAssetPack
from katcha.services.render_recovery import (
    dead_letter_latest_render_attempt,
    ensure_render_attempt,
    mark_render_attempt_retryable_failure,
    mark_render_attempt_started,
    mark_render_attempt_verified,
)


class AmbiguousPaidCall(RuntimeError):
    pass


def _production_cost(session: Any, production_id: uuid.UUID) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.reference_type == "production",
            UsageEvent.reference_id == str(production_id),
        )
    )
    return Decimal(str(value or 0))


def _active_analysis(snapshot: dict[str, Any]) -> dict[str, Any]:
    ai = snapshot.get("ai_features")
    if not isinstance(ai, dict):
        return {}
    deep = ai.get("deep")
    bulk = ai.get("bulk")
    if isinstance(deep, dict):
        return deep
    if isinstance(bulk, dict):
        return bulk
    return {}


def _selected_script(session: Any, production: Production) -> ProductionScript:
    if production.selected_script_id is None:
        raise RuntimeError("production has no selected script")
    script = session.get(ProductionScript, production.selected_script_id)
    if script is None:
        raise RuntimeError("selected production script does not exist")
    return script


def _brand_voice_profiles(
    snapshot: dict[str, Any],
    settings: Settings,
) -> tuple[VoiceProfile | None, VoiceProfile | None]:
    voice_policy = snapshot.get("voice_policy")
    if not isinstance(voice_policy, dict):
        return None, None
    preferred = voice_policy.get("preferred_profiles")
    if not isinstance(preferred, list):
        return None, None

    available: list[VoiceProfile] = []
    for key in preferred:
        try:
            profile = get_voice_profile(str(key))
        except ValueError:
            continue
        if profile.provider == "openai" and settings.openai_api_key:
            available.append(profile)
        elif profile.provider == "gemini" and settings.gemini_api_key:
            available.append(profile)

    primary = available[0] if available else None
    fallback = available[1] if len(available) > 1 else None
    return primary, fallback


def _brand_render_spec(snapshot: dict[str, Any]) -> ShortBrandSpec | None:
    visual = snapshot.get("visual")
    if not isinstance(visual, dict):
        return None
    return ShortBrandSpec.model_validate(visual)


def _frozen_blueprint(production: Production) -> EditBlueprintContract | None:
    snapshot = production.edit_blueprint_snapshot
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    blueprint = EditBlueprintContract.model_validate(snapshot)
    if production.edit_blueprint_key and blueprint.key != production.edit_blueprint_key:
        raise RuntimeError("production edit blueprint key does not match frozen snapshot")
    return blueprint


@activity.defn
def generate_script_candidates(production_id: str) -> dict[str, object]:
    production_uuid = uuid.UUID(production_id)
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        existing = list(
            session.scalars(
                select(ProductionScript)
                .where(ProductionScript.production_id == production_uuid)
                .order_by(ProductionScript.candidate_index)
            )
        )
        if len(existing) == 3:
            return {"production_id": production_id, "candidate_count": 3, "reused": True}
        if production.stage == "script_call_started":
            raise AmbiguousPaidCall(
                "script provider call may already have been accepted; create a new generation "
                "instead of automatically retrying"
            )
        production.status = ProductionStatus.SCRIPTING.value
        production.stage = "script_call_started"
        production.error = None
        persona_key = production.persona_key
        persona_version = production.persona_version
        prompt_version = production.prompt_version
        snapshot = dict(production.analysis_snapshot or {})

    persona = get_persona(persona_key, persona_version)
    result = generate_short_scripts(
        persona,
        snapshot,
        prompt_version=prompt_version,
        production_id=production_id,
    )

    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise RuntimeError("production disappeared after script generation")
        existing_count = session.scalar(
            select(func.count(ProductionScript.id)).where(
                ProductionScript.production_id == production_uuid
            )
        )
        if existing_count:
            return {
                "production_id": production_id,
                "candidate_count": int(existing_count),
                "reused": True,
            }

        for index, candidate in enumerate(result.scripts.candidates):
            session.add(
                ProductionScript(
                    production_id=production_uuid,
                    candidate_index=index,
                    style=candidate.style,
                    narration=candidate.narration,
                    interaction_prompt=candidate.interaction_prompt,
                    rationale=candidate.rationale,
                    provider=result.target.provider,
                    model=result.target.model,
                    prompt_version=prompt_version,
                    selected=False,
                    script_metadata={
                        "segments": [
                            segment.model_dump(mode="json") for segment in candidate.segments
                        ],
                        "title_angle": candidate.title_angle,
                    },
                )
            )
        production.status = ProductionStatus.SCRIPTED.value
        production.stage = "scripts_ready"
        production.estimated_cost_usd = _production_cost(session, production_uuid)
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.scripts_generated",
                payload={
                    "production_id": production_id,
                    "candidate_count": 3,
                    "provider": result.target.provider,
                    "model": result.target.model,
                    "persona_key": persona_key,
                    "persona_version": persona_version,
                    "brand_key": production.brand_key,
                    "brand_version": production.brand_version,
                },
            )
        )
    return {"production_id": production_id, "candidate_count": 3, "reused": False}


@activity.defn
def select_script_candidate(production_id: str) -> dict[str, object]:
    production_uuid = uuid.UUID(production_id)
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        if production.selected_script_id is not None:
            selected = session.get(ProductionScript, production.selected_script_id)
            if selected is not None:
                return {
                    "production_id": production_id,
                    "script_id": str(selected.id),
                    "style": selected.style,
                    "reused": True,
                }

        candidates = list(
            session.scalars(
                select(ProductionScript)
                .where(ProductionScript.production_id == production_uuid)
                .order_by(ProductionScript.candidate_index)
            )
        )
        if len(candidates) != 3:
            raise RuntimeError("exactly three script candidates are required before selection")

        analysis = _active_analysis(dict(production.analysis_snapshot or {}))
        comment_potential = float(analysis.get("comment_potential") or 0)
        humor_score = float(analysis.get("humor_score") or 0)
        if comment_potential >= 75:
            preferred_style = "interactive"
            reason = "high analysis comment-potential signal"
        elif humor_score >= 70:
            preferred_style = "sarcastic"
            reason = "high analysis humor signal"
        else:
            preferred_style = "observational"
            reason = "default low-risk editorial treatment"

        selected = next(
            candidate for candidate in candidates if candidate.style == preferred_style
        )
        for candidate in candidates:
            candidate.selected = candidate.id == selected.id
        production.selected_script_id = selected.id
        production.stage = "script_selected"
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.script_selected",
                payload={
                    "production_id": production_id,
                    "script_id": str(selected.id),
                    "style": selected.style,
                    "selection_reason": reason,
                },
            )
        )
        return {
            "production_id": production_id,
            "script_id": str(selected.id),
            "style": selected.style,
            "selection_reason": reason,
            "reused": False,
        }


def _persist_tts_result(
    production_id: uuid.UUID,
    segment_index: int,
    storage_key: str,
    metadata: dict[str, object],
) -> None:
    with session_scope() as session:
        production = session.get(Production, production_id)
        if production is None:
            raise RuntimeError("production disappeared during TTS persistence")
        voice_profile = str(metadata["voice_profile"])
        if production.selected_voice_profile is None:
            production.selected_voice_profile = voice_profile
        elif production.selected_voice_profile != voice_profile:
            raise RuntimeError("narration voice profile changed within one production")
        existing = session.scalar(
            select(ProductionAsset).where(
                ProductionAsset.production_id == production_id,
                ProductionAsset.kind == f"narration_{segment_index:02d}",
                ProductionAsset.generation == 1,
            )
        )
        if existing is not None:
            return
        session.add(
            ProductionAsset(
                production_id=production_id,
                kind=f"narration_{segment_index:02d}",
                generation=1,
                storage_key=storage_key,
                content_type="audio/wav",
                provider=str(metadata["provider"]),
                model=str(metadata["model"]),
                asset_metadata=metadata,
            )
        )


@activity.defn
def generate_narration_assets(production_id: str) -> dict[str, object]:
    production_uuid = uuid.UUID(production_id)
    settings = get_settings()
    store = ObjectStore()
    store.ensure_bucket()

    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        script = _selected_script(session, production)
        blueprint = _frozen_blueprint(production)
        if blueprint is not None and blueprint.narration.mode in {"text_only", "source_only"}:
            production.status = ProductionStatus.VOICED.value
            production.stage = "voice_skipped_by_blueprint"
            production.error = None
            session.add(
                DomainEvent(
                    aggregate_type="production",
                    aggregate_id=production_id,
                    event_type="production.voice_skipped",
                    payload={
                        "production_id": production_id,
                        "edit_blueprint_key": blueprint.key,
                        "edit_blueprint_version": blueprint.version,
                        "narration_mode": blueprint.narration.mode,
                    },
                )
            )
            return {
                "production_id": production_id,
                "voice_profile": None,
                "generated_segments": 0,
                "reused_segments": 0,
                "skipped": True,
            }
        segments = list((script.script_metadata or {}).get("segments") or [])
        if not segments:
            raise RuntimeError("selected script has no commentary segments")
        brand_snapshot = dict(production.brand_snapshot or {})
        if production.selected_voice_profile:
            profile = get_voice_profile(production.selected_voice_profile)
            fallback_profile = None
        else:
            profile, fallback_profile = _brand_voice_profiles(
                brand_snapshot, settings
            )
        channel_profile_id = production.channel_profile_id
        try:
            expected_value = max(
                0.0,
                min(
                    1.0,
                    float((production.analysis_snapshot or {}).get("candidate_score") or 0)
                    / 100.0,
                ),
            )
        except (TypeError, ValueError):
            expected_value = 0.5
        production.status = ProductionStatus.VOICING.value
        production.error = None

    generated = 0
    reused = 0
    for index, segment in enumerate(segments):
        kind = f"narration_{index:02d}"
        with session_scope() as session:
            existing = session.scalar(
                select(ProductionAsset).where(
                    ProductionAsset.production_id == production_uuid,
                    ProductionAsset.kind == kind,
                    ProductionAsset.generation == 1,
                )
            )
            if existing is not None:
                if profile is None:
                    saved_profile = (existing.asset_metadata or {}).get("voice_profile")
                    if saved_profile:
                        profile = get_voice_profile(str(saved_profile))
                reused += 1
                continue
            production = session.get(Production, production_uuid)
            if production is None:
                raise RuntimeError("production disappeared before TTS")
            call_stage = f"tts_call_started_{index:02d}"
            audio_key = store.production_key(
                production_id, f"audio/segment-{index:02d}-g1.wav"
            )
            sidecar_key = store.production_key(
                production_id, f"audio/segment-{index:02d}-g1.json"
            )
            if production.stage == call_stage:
                if store.exists(audio_key) and store.exists(sidecar_key):
                    metadata = json.loads(store.get_bytes(sidecar_key).decode("utf-8"))
                    _persist_tts_result(production_uuid, index, audio_key, metadata)
                    profile = get_voice_profile(str(metadata["voice_profile"]))
                    reused += 1
                    continue
                raise AmbiguousPaidCall(
                    f"TTS call for segment {index} may already have been accepted; "
                    "regenerate the voice stage instead of automatically retrying"
                )
            production.stage = call_stage

        text = str(segment.get("text") or "").strip()
        result = synthesize_speech(
            text,
            profile=profile,
            fallback_profile=fallback_profile,
            settings=settings,
            channel_profile_id=channel_profile_id,
            reference_type="production",
            reference_id=production_id,
            reservation_key=f"tts:production:{production_id}:{index:02d}",
            expected_value=expected_value,
            usage_metadata={"segment_index": index},
        )
        profile = result.profile
        fallback_profile = None
        metadata: dict[str, object] = {
            "segment_index": index,
            "placement": segment.get("placement"),
            "source_time_seconds": segment.get("source_time_seconds"),
            "text": text,
            "duration_seconds": round(result.duration_seconds, 3),
            "voice_profile": result.profile.key,
            "voice_profile_version": result.profile.version,
            "voice": result.profile.voice,
            "provider": result.target.provider,
            "model": result.target.model,
            "input_units": result.input_units,
            "output_units": result.output_units,
            "cost_usd": str(result.estimated_cost_usd),
            "cost_metadata": result.cost_metadata,
        }
        store.put_bytes(result.audio, audio_key, content_type=result.content_type)
        store.put_bytes(
            json.dumps(metadata, sort_keys=True).encode("utf-8"),
            sidecar_key,
            content_type="application/json",
        )
        _persist_tts_result(production_uuid, index, audio_key, metadata)
        generated += 1

    if profile is None:
        raise RuntimeError("production narration has no recoverable voice profile")
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise RuntimeError("production disappeared after TTS")
        production.status = ProductionStatus.VOICED.value
        production.stage = "voice_ready"
        production.estimated_cost_usd = _production_cost(session, production_uuid)
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.voice_ready",
                payload={
                    "production_id": production_id,
                    "brand_key": production.brand_key,
                    "brand_version": production.brand_version,
                    "voice_profile": production.selected_voice_profile,
                    "generated_segments": generated,
                    "reused_segments": reused,
                },
            )
        )
    return {
        "production_id": production_id,
        "voice_profile": profile.key,
        "generated_segments": generated,
        "reused_segments": reused,
    }


@activity.defn
def build_render_manifest_activity(production_id: str) -> dict[str, object]:
    production_uuid = uuid.UUID(production_id)
    settings = get_settings()
    store = ObjectStore()
    store.ensure_bucket()

    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        clip = session.get(Clip, production.clip_id)
        if clip is None:
            raise RuntimeError("production source clip does not exist")
        if not store.exists(clip.storage_key):
            raise RuntimeError("production source object is missing from storage")
        script = _selected_script(session, production)
        blueprint = _frozen_blueprint(production)
        brand = _brand_render_spec(dict(production.brand_snapshot or {}))
        output_key = store.production_key(production_id, "render/short-g1.mp4")

        if (
            blueprint is not None
            and blueprint.composition == "blueprint_video"
            and blueprint.narration.mode in {"text_only", "source_only"}
        ):
            if production.channel_profile_id is None:
                raise RuntimeError("blueprint render requires channel-scoped production")
            if brand is None:
                raise RuntimeError("blueprint render requires frozen brand visual tokens")
            narration_asset_key = None
            narration_text = None
            narration_duration_seconds = None
            if blueprint.narration.mode not in {"text_only", "source_only"}:
                narration_assets = list(
                    session.scalars(
                        select(ProductionAsset)
                        .where(
                            ProductionAsset.production_id == production_uuid,
                            ProductionAsset.kind.like("narration_%"),
                        )
                        .order_by(ProductionAsset.kind)
                    )
                )
                if len(narration_assets) != 1:
                    raise RuntimeError(
                        "blueprint voice render currently requires exactly one narration asset"
                    )
                narration_asset = narration_assets[0]
                if not store.exists(narration_asset.storage_key):
                    raise RuntimeError("blueprint narration object is missing from storage")
                narration_asset_key = narration_asset.storage_key
                narration_text = script.narration
                narration_duration_seconds = float(
                    (narration_asset.asset_metadata or {}).get("duration_seconds") or 0
                )
            headline = str((script.script_metadata or {}).get("title_angle") or "").strip()
            manifest = build_blueprint_render_manifest(
                render_id=production_id,
                channel_profile_id=str(production.channel_profile_id),
                brand=brand,
                blueprint=blueprint,
                source_storage_key=clip.storage_key,
                source_duration_seconds=float(clip.duration_seconds or 0),
                output_key=output_key,
                headline=(headline or None) if blueprint.header.required else None,
                narration_asset_key=narration_asset_key,
                narration_text=narration_text,
                narration_duration_seconds=narration_duration_seconds,
                width=settings.render_width,
                height=settings.render_height,
                fps=settings.render_fps,
            )
            caption_payload = (
                [cue.model_dump(mode="json") for cue in manifest.narration.cues]
                if manifest.narration is not None
                else []
            )
        else:
            segments = list((script.script_metadata or {}).get("segments") or [])
            narration_assets = list(
                session.scalars(
                    select(ProductionAsset)
                    .where(
                        ProductionAsset.production_id == production_uuid,
                        ProductionAsset.kind.like("narration_%"),
                    )
                    .order_by(ProductionAsset.kind)
                )
            )
            if len(narration_assets) != len(segments):
                raise RuntimeError("narration assets are incomplete")
            asset_payload = [
                {
                    "storage_key": asset.storage_key,
                    "segment_index": int(
                        (asset.asset_metadata or {}).get("segment_index", index)
                    ),
                    "duration_seconds": float(
                        (asset.asset_metadata or {}).get("duration_seconds") or 0
                    ),
                }
                for index, asset in enumerate(narration_assets)
            ]
            for asset in narration_assets:
                if not store.exists(asset.storage_key):
                    raise RuntimeError("narration object is missing from storage")
            visual = dict((production.brand_snapshot or {}).get("visual") or {})
            reaction_pack_payload = visual.get("reaction_pack")
            reaction_pack = (
                ReactionAssetPack.model_validate(reaction_pack_payload)
                if reaction_pack_payload is not None
                else None
            )
            manifest = build_short_manifest(
                production_id=production_id,
                source_key=clip.storage_key,
                source_duration_seconds=float(clip.duration_seconds or 0),
                source_width=clip.width,
                source_height=clip.height,
                source_audio_volume=settings.source_audio_volume,
                width=settings.render_width,
                height=settings.render_height,
                fps=settings.render_fps,
                script_segments=segments,
                narration_assets=asset_payload,
                output_key=output_key,
                title_angle=str((script.script_metadata or {}).get("title_angle") or "")
                or None,
                interaction_prompt=script.interaction_prompt,
                brand=brand,
                reaction_pack=reaction_pack,
                reaction_cues=list(
                    (script.script_metadata or {}).get("reaction_cues") or []
                ),
            )
            caption_payload = [
                cue.model_dump(mode="json")
                for overlay in manifest.overlays
                for cue in overlay.cues
            ]

    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    manifest_key = store.production_key(production_id, "render/manifest.json")
    store.put_bytes(manifest_bytes, manifest_key, content_type="application/json")
    captions_key = store.production_key(production_id, "render/captions.json")
    store.put_bytes(
        json.dumps(caption_payload, indent=2).encode("utf-8"),
        captions_key,
        content_type="application/json",
    )

    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise RuntimeError("production disappeared while persisting manifest")
        production.render_manifest = manifest.model_dump(mode="json")
        production.status = ProductionStatus.RENDERING.value
        production.stage = "manifest_ready"
        production.error = None
        for kind, key in (("manifest", manifest_key), ("captions", captions_key)):
            existing = session.scalar(
                select(ProductionAsset).where(
                    ProductionAsset.production_id == production_uuid,
                    ProductionAsset.kind == kind,
                    ProductionAsset.generation == 1,
                )
            )
            if existing is None:
                session.add(
                    ProductionAsset(
                        production_id=production_uuid,
                        kind=kind,
                        generation=1,
                        storage_key=key,
                        content_type="application/json",
                        asset_metadata={
                            "manifest_version": manifest.version,
                            "brand_key": production.brand_key,
                            "brand_version": production.brand_version,
                            "edit_blueprint_key": production.edit_blueprint_key,
                            "edit_blueprint_version": production.edit_blueprint_version,
                        },
                    )
                )
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.render_manifest_ready",
                payload={
                    "production_id": production_id,
                    "manifest_key": manifest_key,
                    "manifest_version": manifest.version,
                    "brand_key": production.brand_key,
                    "brand_version": production.brand_version,
                    "edit_blueprint_key": production.edit_blueprint_key,
                    "edit_blueprint_version": production.edit_blueprint_version,
                    "output_duration_seconds": manifest.output_duration_seconds,
                },
            )
        )
    return {
        "production_id": production_id,
        "manifest_key": manifest_key,
        "output_key": manifest.output_key,
        "manifest_version": manifest.version,
    }


@activity.defn
def render_short_activity(production_id: str) -> dict[str, object]:
    production_uuid = uuid.UUID(production_id)
    settings = get_settings()
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise ValueError(f"production not found: {production_id}")
        if not production.render_manifest:
            raise RuntimeError("render manifest is missing")
        manifest_payload = dict(production.render_manifest)
        if manifest_payload.get("version") == "blueprint-render-v1":
            manifest = BlueprintRenderManifest.model_validate(manifest_payload)
            if production.edit_blueprint_key != manifest.blueprint_key:
                raise RuntimeError("render blueprint lineage does not match production")
            render_fn = render_blueprint
        else:
            manifest = ShortRenderManifest.model_validate(manifest_payload)
            render_fn = render_short
        existing = session.scalar(
            select(ProductionAsset).where(
                ProductionAsset.production_id == production_uuid,
                ProductionAsset.kind == "render",
                ProductionAsset.generation == 1,
            )
        )
        existing_metadata = dict(existing.asset_metadata or {}) if existing else {}
        existing_key = existing.storage_key if existing is not None else None
        production.stage = "rendering"

    attempt = ensure_render_attempt(
        "production",
        production_uuid,
        output_key=manifest.output_key,
        manifest_version=manifest.version,
    )
    if existing_key is not None:
        if not bool(existing_metadata.get("verified")):
            raise RuntimeError("existing render asset is not post-render verified")
        mark_render_attempt_verified(attempt.id, verification=existing_metadata)
        with session_scope() as session:
            production = session.get(Production, production_uuid)
            if production is not None:
                production.status = ProductionStatus.REVIEW.value
                production.stage = "render_verified"
                production.error = None
        return {
            "production_id": production_id,
            "output_key": existing_key,
            "reused": True,
            "verified": True,
            "render_attempt_id": str(attempt.id),
        }

    mark_render_attempt_started(attempt.id)
    try:
        result = render_fn(manifest, settings=settings)
        if not bool((result.metadata or {}).get("verified")):
            raise RuntimeError("renderer output failed post-render verification")
    except Exception as exc:
        mark_render_attempt_retryable_failure(
            attempt.id,
            failure_class=type(exc).__name__,
            error=str(exc),
        )
        raise

    verification = {
        **dict(result.metadata or {}),
        "duration_seconds": result.duration_seconds,
        "output_key": result.output_key,
    }
    mark_render_attempt_verified(attempt.id, verification=verification)

    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            raise RuntimeError("production disappeared after render")
        existing = session.scalar(
            select(ProductionAsset).where(
                ProductionAsset.production_id == production_uuid,
                ProductionAsset.kind == "render",
                ProductionAsset.generation == 1,
            )
        )
        if existing is None:
            session.add(
                ProductionAsset(
                    production_id=production_uuid,
                    kind="render",
                    generation=1,
                    storage_key=result.output_key,
                    content_type="video/mp4",
                    provider="remotion",
                    model=str((result.metadata or {}).get("composition") or "") or None,
                    asset_metadata={
                        "duration_seconds": result.duration_seconds,
                        "brand_key": production.brand_key,
                        "brand_version": production.brand_version,
                        "edit_blueprint_key": production.edit_blueprint_key,
                        "edit_blueprint_version": production.edit_blueprint_version,
                        "render_attempt_id": str(attempt.id),
                        **result.metadata,
                    },
                )
            )
        production.status = ProductionStatus.REVIEW.value
        production.stage = "render_verified"
        production.error = None
        production.estimated_cost_usd = _production_cost(session, production_uuid)
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.render_verified",
                payload={
                    "production_id": production_id,
                    "render_attempt_id": str(attempt.id),
                    "output_key": result.output_key,
                    "duration_seconds": result.duration_seconds,
                    "verification": result.metadata,
                },
            )
        )
    return {
        "production_id": production_id,
        "output_key": result.output_key,
        "reused": False,
        "verified": True,
        "render_attempt_id": str(attempt.id),
    }


@activity.defn
def mark_production_failed(production_id: str, message: str) -> None:
    production_uuid = uuid.UUID(production_id)
    with session_scope() as session:
        production = session.get(Production, production_uuid)
        if production is None:
            return
        production.status = ProductionStatus.FAILED.value
        production.stage = "failed"
        production.error = message[:8000]
        production.estimated_cost_usd = _production_cost(session, production_uuid)
        session.add(
            DomainEvent(
                aggregate_type="production",
                aggregate_id=production_id,
                event_type="production.failed",
                payload={"production_id": production_id, "error": production.error},
            )
        )
    dead_letter_latest_render_attempt(
        "production",
        production_uuid,
        error=message,
    )
