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
from katcha.editorial.episode_generator import generate_ranked_episode_scripts
from katcha.editorial.personas import get_persona
from katcha.integrations.storage import ObjectStore
from katcha.models import DomainEvent, UsageEvent
from katcha.services.render_recovery import dead_letter_latest_render_attempt
from katcha.short_episode_models import (
    ShortEpisode,
    ShortEpisodeAsset,
    ShortEpisodeItem,
    ShortEpisodeScript,
)


class AmbiguousEpisodePaidCall(RuntimeError):
    pass


def _episode_cost(session: Any, episode_id: uuid.UUID) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.reference_type == "short_episode",
            UsageEvent.reference_id == str(episode_id),
        )
    )
    return Decimal(str(value or 0))


def _episode_item_payload(item: ShortEpisodeItem) -> dict[str, object]:
    return {
        "position": item.position,
        "clip_id": str(item.clip_id),
        "role": item.role,
        "overall_score": float(item.overall_score),
        "role_score": float(item.role_score),
        "editorial_signals": dict(item.editorial_signals or {}),
        "analysis_snapshot": dict(item.analysis_snapshot or {}),
        "acquisition_snapshot": dict(item.acquisition_snapshot or {}),
        "source_snapshot": list(item.source_snapshot or []),
    }


def _expected_value(items: list[dict[str, object]]) -> float:
    if not items:
        return 0.5
    scores = [float(item.get("overall_score") or 0) for item in items]
    return max(0.0, min(1.0, sum(scores) / len(scores) / 100.0))


def _analysis_metric(snapshot: dict[str, Any], key: str) -> float:
    ai = snapshot.get("ai_features")
    if not isinstance(ai, dict):
        return 0.0
    for layer in ("deep", "bulk"):
        value = ai.get(layer)
        if isinstance(value, dict):
            try:
                return float(value.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _preferred_style(items: list[ShortEpisodeItem]) -> tuple[str, str]:
    if not items:
        return "observational", "safe default for an episode with no item signals"
    commentary_values = [
        float((item.editorial_signals or {}).get("commentary_opportunity") or 0)
        for item in items
    ]
    humor_values = [
        _analysis_metric(dict(item.analysis_snapshot or {}), "humor_score") for item in items
    ]
    avg_commentary = sum(commentary_values) / len(commentary_values)
    avg_humor = sum(humor_values) / len(humor_values)
    if avg_commentary >= 82:
        return "interactive", "high average commentary-opportunity signal"
    if avg_humor >= 70:
        return "sarcastic", "high average humor signal"
    return "observational", "default grounded countdown treatment"


def _brand_voice_profile(
    snapshot: dict[str, Any],
    settings: Settings,
) -> VoiceProfile | None:
    voice_policy = snapshot.get("voice_policy")
    if not isinstance(voice_policy, dict):
        return None
    preferred = voice_policy.get("preferred_profiles")
    if not isinstance(preferred, list):
        return None
    for key in preferred:
        try:
            profile = get_voice_profile(str(key))
        except ValueError:
            continue
        if profile.provider == "openai" and settings.openai_api_key:
            return profile
        if profile.provider == "gemini" and settings.gemini_api_key:
            return profile
    return None


def _selected_script(session: Any, episode: ShortEpisode) -> ShortEpisodeScript:
    if episode.selected_script_id is None:
        raise RuntimeError("short episode has no selected script")
    script = session.get(ShortEpisodeScript, episode.selected_script_id)
    if script is None:
        raise RuntimeError("selected short episode script does not exist")
    return script


@activity.defn
def generate_episode_script_candidates(episode_id: str) -> dict[str, object]:
    episode_uuid = uuid.UUID(episode_id)
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        existing = list(
            session.scalars(
                select(ShortEpisodeScript)
                .where(ShortEpisodeScript.short_episode_id == episode_uuid)
                .order_by(ShortEpisodeScript.candidate_index)
            )
        )
        if len(existing) == 3:
            return {"episode_id": episode_id, "candidate_count": 3, "reused": True}
        if episode.stage == "script_call_started":
            raise AmbiguousEpisodePaidCall(
                "episode script call may already have been accepted; regenerate from script "
                "instead of automatically retrying"
            )
        items = list(
            session.scalars(
                select(ShortEpisodeItem)
                .where(ShortEpisodeItem.short_episode_id == episode_uuid)
                .order_by(ShortEpisodeItem.position.desc())
            )
        )
        if len(items) != episode.item_count:
            raise RuntimeError("short episode item plan is incomplete")
        item_payloads = [_episode_item_payload(item) for item in items]
        episode.status = "scripting"
        episode.stage = "script_call_started"
        episode.error = None
        persona_key = episode.persona_key
        persona_version = episode.persona_version
        premise = episode.premise
        plan_snapshot = dict(episode.plan_snapshot or {})
        prompt_version = episode.prompt_version
        channel_profile_id = episode.channel_profile_id
        expected_value = _expected_value(item_payloads)

    persona = get_persona(persona_key, persona_version)
    result = generate_ranked_episode_scripts(
        persona,
        premise=premise,
        plan_snapshot=plan_snapshot,
        items=item_payloads,
        prompt_version=prompt_version,
        episode_id=episode_id,
        channel_profile_id=channel_profile_id,
        expected_value=expected_value,
    )

    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise RuntimeError("short episode disappeared after script generation")
        existing_count = session.scalar(
            select(func.count(ShortEpisodeScript.id)).where(
                ShortEpisodeScript.short_episode_id == episode_uuid
            )
        )
        if existing_count:
            return {
                "episode_id": episode_id,
                "candidate_count": int(existing_count),
                "reused": True,
            }
        for index, candidate in enumerate(result.scripts.candidates):
            session.add(
                ShortEpisodeScript(
                    short_episode_id=episode_uuid,
                    candidate_index=index,
                    style=candidate.style,
                    script_payload=candidate.model_dump(mode="json"),
                    narration_beats=candidate.narration_beats(),
                    provider=result.target.provider,
                    model=result.target.model,
                    prompt_version=prompt_version,
                    selected=False,
                )
            )
        episode.status = "scripted"
        episode.stage = "scripts_ready"
        episode.estimated_cost_usd = _episode_cost(session, episode_uuid)
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=episode_id,
                event_type="short_episode.scripts_generated",
                payload={
                    "short_episode_id": episode_id,
                    "candidate_count": 3,
                    "provider": result.target.provider,
                    "model": result.target.model,
                    "prompt_version": prompt_version,
                    "brand_key": episode.brand_key,
                    "brand_version": episode.brand_version,
                },
            )
        )
    return {"episode_id": episode_id, "candidate_count": 3, "reused": False}


@activity.defn
def select_episode_script_candidate(episode_id: str) -> dict[str, object]:
    episode_uuid = uuid.UUID(episode_id)
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        if episode.selected_script_id is not None:
            selected = session.get(ShortEpisodeScript, episode.selected_script_id)
            if selected is not None:
                return {
                    "episode_id": episode_id,
                    "script_id": str(selected.id),
                    "style": selected.style,
                    "reused": True,
                }
        candidates = list(
            session.scalars(
                select(ShortEpisodeScript)
                .where(ShortEpisodeScript.short_episode_id == episode_uuid)
                .order_by(ShortEpisodeScript.candidate_index)
            )
        )
        if len(candidates) != 3:
            raise RuntimeError("three complete episode treatments are required before selection")
        items = list(
            session.scalars(
                select(ShortEpisodeItem).where(
                    ShortEpisodeItem.short_episode_id == episode_uuid
                )
            )
        )
        preferred_style, reason = _preferred_style(items)
        selected = next(
            candidate for candidate in candidates if candidate.style == preferred_style
        )
        for candidate in candidates:
            candidate.selected = candidate.id == selected.id
        episode.selected_script_id = selected.id
        episode.stage = "script_selected"
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=episode_id,
                event_type="short_episode.script_selected",
                payload={
                    "short_episode_id": episode_id,
                    "script_id": str(selected.id),
                    "style": selected.style,
                    "selection_reason": reason,
                },
            )
        )
        return {
            "episode_id": episode_id,
            "script_id": str(selected.id),
            "style": selected.style,
            "selection_reason": reason,
            "reused": False,
        }


def _persist_episode_tts(
    episode_id: uuid.UUID,
    sequence: int,
    storage_key: str,
    metadata: dict[str, object],
) -> None:
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_id)
        if episode is None:
            raise RuntimeError("short episode disappeared during TTS persistence")
        voice_profile = str(metadata["voice_profile"])
        if episode.selected_voice_profile is None:
            episode.selected_voice_profile = voice_profile
        elif episode.selected_voice_profile != voice_profile:
            raise RuntimeError("narration voice profile changed within one short episode")
        kind = f"narration_{sequence:02d}"
        existing = session.scalar(
            select(ShortEpisodeAsset).where(
                ShortEpisodeAsset.short_episode_id == episode_id,
                ShortEpisodeAsset.kind == kind,
                ShortEpisodeAsset.generation == 1,
            )
        )
        if existing is not None:
            return
        session.add(
            ShortEpisodeAsset(
                short_episode_id=episode_id,
                kind=kind,
                generation=1,
                storage_key=storage_key,
                content_type="audio/wav",
                provider=str(metadata["provider"]),
                model=str(metadata["model"]),
                asset_metadata=metadata,
            )
        )


@activity.defn
def generate_episode_narration_assets(episode_id: str) -> dict[str, object]:
    episode_uuid = uuid.UUID(episode_id)
    settings = get_settings()
    store = ObjectStore()
    store.ensure_bucket()

    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise ValueError(f"short episode not found: {episode_id}")
        script = _selected_script(session, episode)
        beats = list(script.narration_beats or [])
        if not beats:
            raise RuntimeError("selected episode script has no narration beats")
        profile = (
            get_voice_profile(episode.selected_voice_profile)
            if episode.selected_voice_profile
            else _brand_voice_profile(dict(episode.brand_snapshot or {}), settings)
        )
        channel_profile_id = episode.channel_profile_id
        items = list(
            session.scalars(
                select(ShortEpisodeItem).where(
                    ShortEpisodeItem.short_episode_id == episode_uuid
                )
            )
        )
        expected_value = _expected_value([_episode_item_payload(item) for item in items])
        episode.status = "voicing"
        episode.error = None

    generated = 0
    reused = 0
    for beat in beats:
        sequence = int(beat["sequence"])
        kind = f"narration_{sequence:02d}"
        audio_key = store.short_episode_key(
            episode_id, f"audio/beat-{sequence:02d}-g1.wav"
        )
        sidecar_key = store.short_episode_key(
            episode_id, f"audio/beat-{sequence:02d}-g1.json"
        )
        with session_scope() as session:
            existing = session.scalar(
                select(ShortEpisodeAsset).where(
                    ShortEpisodeAsset.short_episode_id == episode_uuid,
                    ShortEpisodeAsset.kind == kind,
                    ShortEpisodeAsset.generation == 1,
                )
            )
            if existing is not None:
                if profile is None:
                    saved_profile = (existing.asset_metadata or {}).get("voice_profile")
                    if saved_profile:
                        profile = get_voice_profile(str(saved_profile))
                reused += 1
                continue
            episode = session.get(ShortEpisode, episode_uuid)
            if episode is None:
                raise RuntimeError("short episode disappeared before TTS")
            call_stage = f"tts_call_started_{sequence:02d}"
            if episode.stage == call_stage:
                if store.exists(audio_key) and store.exists(sidecar_key):
                    metadata = json.loads(store.get_bytes(sidecar_key).decode("utf-8"))
                    _persist_episode_tts(episode_uuid, sequence, audio_key, metadata)
                    profile = get_voice_profile(str(metadata["voice_profile"]))
                    reused += 1
                    continue
                raise AmbiguousEpisodePaidCall(
                    f"episode TTS call {sequence} may already have been accepted; regenerate "
                    "the voice stage instead of automatically retrying"
                )
            episode.stage = call_stage

        text = str(beat.get("text") or "").strip()
        result = synthesize_speech(
            text,
            profile=profile,
            settings=settings,
            channel_profile_id=channel_profile_id,
            reference_type="short_episode",
            reference_id=episode_id,
            reservation_key=f"tts:short-episode:{episode_id}:{sequence:02d}",
            expected_value=expected_value,
            usage_metadata={
                "sequence": sequence,
                "placement": beat.get("placement"),
                "position": beat.get("position"),
                "clip_id": beat.get("clip_id"),
            },
        )
        profile = result.profile
        metadata: dict[str, object] = {
            **dict(beat),
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
        _persist_episode_tts(episode_uuid, sequence, audio_key, metadata)
        generated += 1

    if profile is None:
        raise RuntimeError("short episode narration has no recoverable voice profile")
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            raise RuntimeError("short episode disappeared after TTS")
        episode.status = "voiced"
        episode.stage = "voice_ready"
        episode.estimated_cost_usd = _episode_cost(session, episode_uuid)
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=episode_id,
                event_type="short_episode.voice_ready",
                payload={
                    "short_episode_id": episode_id,
                    "voice_profile": episode.selected_voice_profile,
                    "generated_beats": generated,
                    "reused_beats": reused,
                },
            )
        )
    return {
        "episode_id": episode_id,
        "voice_profile": profile.key,
        "generated_beats": generated,
        "reused_beats": reused,
    }


@activity.defn
def mark_short_episode_failed(episode_id: str, error: str) -> None:
    episode_uuid = uuid.UUID(episode_id)
    with session_scope() as session:
        episode = session.get(ShortEpisode, episode_uuid)
        if episode is None:
            return
        episode.status = "failed"
        episode.stage = "failed"
        episode.error = error[:4000]
        episode.estimated_cost_usd = _episode_cost(session, episode_uuid)
        session.add(
            DomainEvent(
                aggregate_type="short_episode",
                aggregate_id=episode_id,
                event_type="short_episode.failed",
                payload={"short_episode_id": episode_id, "error": error[:1000]},
            )
        )
    dead_letter_latest_render_attempt(
        "short_episode",
        episode_uuid,
        error=error,
    )
