from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from katcha.audio.tts import choose_voice_profile, get_voice_profile, synthesize_speech
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AITask, CompilationStatus
from katcha.editorial.personas import get_persona
from katcha.integrations.storage import ObjectStore
from katcha.longform.candidates import select_compilation_candidates
from katcha.longform.editor import (
    LongformAIResult,
    critique_editor_plan,
    finalize_editor_plan,
    generate_editor_plan,
)
from katcha.longform.schemas import (
    CandidateEvidence,
    FinalLongformPlan,
    LongformCritique,
    LongformEditorPlan,
)
from katcha.longform_models import Compilation, CompilationAsset, CompilationSegment
from katcha.models import Clip, DomainEvent, UsageEvent
from katcha.rendering.client import render_longform
from katcha.rendering.longform_manifest import (
    LongformRenderManifest,
    build_longform_manifest,
)


class AmbiguousLongformPaidCall(RuntimeError):
    pass


def _compilation_cost(session: Any, compilation_id: uuid.UUID) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
            UsageEvent.reference_type == "compilation",
            UsageEvent.reference_id == str(compilation_id),
        )
    )
    return Decimal(str(value or 0))


def _candidate_list(compilation: Compilation) -> list[CandidateEvidence]:
    raw = (compilation.candidate_snapshot or {}).get("candidates")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("compilation has no frozen candidate evidence")
    return [CandidateEvidence.model_validate(item) for item in raw]


def _longform_result_value(result: LongformAIResult) -> object:
    return result.value


def _provider_payload(result: LongformAIResult | FinalLongformPlan) -> dict[str, str]:
    if isinstance(result, LongformAIResult):
        return {"provider": result.target.provider, "model": result.target.model}
    return {"provider": "local", "model": "critic-pass"}


@activity.defn
def select_compilation_candidates_activity(compilation_id: str) -> dict[str, object]:
    compilation_uuid = uuid.UUID(compilation_id)
    settings = get_settings()
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if (compilation.candidate_snapshot or {}).get("candidates"):
            return {
                "compilation_id": compilation_id,
                "candidate_count": len(compilation.candidate_snapshot["candidates"]),
                "reused": True,
            }
        compilation.status = CompilationStatus.SELECTING.value
        compilation.stage = "selecting_candidates"
        compilation.error = None
        candidates = select_compilation_candidates(
            session,
            theme=compilation.theme,
            target_duration_seconds=compilation.target_duration_seconds,
            target_segment_count=compilation.target_segment_count,
            settings=settings,
        )
        payload = [item.model_dump(mode="json") for item in candidates]
        measured = sum(bool(item.evidence.get("measured_short")) for item in candidates)
        compilation.candidate_snapshot = {
            "version": "candidate-snapshot-v1",
            "candidates": payload,
            "measured_candidate_count": measured,
            "selection_policy": "deterministic-performance-diversity-v1",
        }
        compilation.status = CompilationStatus.PLANNING.value
        compilation.stage = "candidates_selected"
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=compilation_id,
                event_type="compilation.candidates_selected",
                payload={
                    "compilation_id": compilation_id,
                    "candidate_count": len(candidates),
                    "measured_candidate_count": measured,
                },
            )
        )
        return {
            "compilation_id": compilation_id,
            "candidate_count": len(candidates),
            "measured_candidate_count": measured,
            "reused": False,
        }


@activity.defn
def generate_longform_editor_plan_activity(compilation_id: str) -> dict[str, object]:
    compilation_uuid = uuid.UUID(compilation_id)
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if compilation.editor_plan:
            return {"compilation_id": compilation_id, "reused": True}
        if compilation.stage == "editor_call_started":
            raise AmbiguousLongformPaidCall(
                "long-form editor call may already have been accepted; regenerate the plan "
                "generation instead of automatically retrying"
            )
        candidates = _candidate_list(compilation)
        compilation.status = CompilationStatus.PLANNING.value
        compilation.stage = "editor_call_started"
        theme = compilation.theme
        target_duration = compilation.target_duration_seconds
        persona_key = compilation.persona_key
        persona_version = compilation.persona_version
        prompt_version = compilation.prompt_version

    settings = get_settings()
    persona = get_persona(persona_key, persona_version)
    result = generate_editor_plan(
        theme=theme,
        target_duration_seconds=target_duration,
        persona=persona,
        candidates=candidates,
        prompt_version=prompt_version,
        compilation_id=compilation_id,
        min_segments=settings.longform_min_segments,
        settings=settings,
    )
    plan = _longform_result_value(result)
    if not isinstance(plan, LongformEditorPlan):
        raise RuntimeError("long-form editor returned the wrong schema")

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise RuntimeError("compilation disappeared after editor call")
        if not compilation.editor_plan:
            compilation.editor_plan = plan.model_dump(mode="json")
            compilation.status = CompilationStatus.CRITIQUING.value
            compilation.stage = "editor_plan_ready"
            compilation.estimated_cost_usd = _compilation_cost(session, compilation_uuid)
            session.add(
                DomainEvent(
                    aggregate_type="compilation",
                    aggregate_id=compilation_id,
                    event_type="compilation.editor_plan_ready",
                    payload={
                        "compilation_id": compilation_id,
                        "provider": result.target.provider,
                        "model": result.target.model,
                        "segment_count": len(plan.segments),
                    },
                )
            )
    return {
        "compilation_id": compilation_id,
        "provider": result.target.provider,
        "model": result.target.model,
        "segment_count": len(plan.segments),
        "reused": False,
    }


@activity.defn
def critique_longform_plan_activity(compilation_id: str) -> dict[str, object]:
    compilation_uuid = uuid.UUID(compilation_id)
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if compilation.critic_feedback:
            return {"compilation_id": compilation_id, "reused": True}
        if compilation.stage == "critic_call_started":
            raise AmbiguousLongformPaidCall(
                "long-form critic call may already have been accepted; regenerate the plan "
                "generation instead of automatically retrying"
            )
        if not compilation.editor_plan:
            raise RuntimeError("editor plan is required before critique")
        candidates = _candidate_list(compilation)
        plan = LongformEditorPlan.model_validate(compilation.editor_plan)
        compilation.status = CompilationStatus.CRITIQUING.value
        compilation.stage = "critic_call_started"
        theme = compilation.theme

    result = critique_editor_plan(
        theme=theme,
        candidates=candidates,
        plan=plan,
        compilation_id=compilation_id,
    )
    critique = _longform_result_value(result)
    if not isinstance(critique, LongformCritique):
        raise RuntimeError("long-form critic returned the wrong schema")

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise RuntimeError("compilation disappeared after critic call")
        if not compilation.critic_feedback:
            compilation.critic_feedback = critique.model_dump(mode="json")
            compilation.stage = "critic_ready"
            compilation.estimated_cost_usd = _compilation_cost(session, compilation_uuid)
            session.add(
                DomainEvent(
                    aggregate_type="compilation",
                    aggregate_id=compilation_id,
                    event_type="compilation.critic_ready",
                    payload={
                        "compilation_id": compilation_id,
                        "provider": result.target.provider,
                        "model": result.target.model,
                        "verdict": critique.verdict,
                        "issue_count": len(critique.issues),
                    },
                )
            )
    return {
        "compilation_id": compilation_id,
        "verdict": critique.verdict,
        "issue_count": len(critique.issues),
        "reused": False,
    }


@activity.defn
def finalize_longform_plan_activity(compilation_id: str) -> dict[str, object]:
    compilation_uuid = uuid.UUID(compilation_id)
    settings = get_settings()
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if compilation.final_plan:
            count = session.scalar(
                select(func.count(CompilationSegment.id)).where(
                    CompilationSegment.compilation_id == compilation_uuid
                )
            )
            if count:
                return {
                    "compilation_id": compilation_id,
                    "segment_count": int(count),
                    "reused": True,
                }
        if not compilation.editor_plan or not compilation.critic_feedback:
            raise RuntimeError("editor plan and critic feedback are required before finalization")
        candidates = _candidate_list(compilation)
        plan = LongformEditorPlan.model_validate(compilation.editor_plan)
        critique = LongformCritique.model_validate(compilation.critic_feedback)
        if critique.verdict == "revise" and compilation.stage == "revision_call_started":
            raise AmbiguousLongformPaidCall(
                "long-form revision call may already have been accepted; regenerate the plan "
                "generation instead of automatically retrying"
            )
        if critique.verdict == "revise":
            compilation.stage = "revision_call_started"
        theme = compilation.theme
        persona_key = compilation.persona_key
        persona_version = compilation.persona_version

    persona = get_persona(persona_key, persona_version)
    result = finalize_editor_plan(
        theme=theme,
        persona=persona,
        candidates=candidates,
        plan=plan,
        critique=critique,
        compilation_id=compilation_id,
        min_segments=settings.longform_min_segments,
        settings=settings,
    )
    if isinstance(result, LongformAIResult):
        final = _longform_result_value(result)
        provider = _provider_payload(result)
    else:
        final = result
        provider = _provider_payload(result)
    if not isinstance(final, FinalLongformPlan):
        raise RuntimeError("long-form finalizer returned the wrong schema")
    evidence_by_clip = {item.clip_id: item for item in candidates}

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise RuntimeError("compilation disappeared after final plan")
        compilation.final_plan = final.model_dump(mode="json")
        session.execute(
            delete(CompilationSegment).where(
                CompilationSegment.compilation_id == compilation_uuid
            )
        )
        for position, item in enumerate(final.segments):
            evidence = evidence_by_clip[item.clip_id]
            end = item.source_end_seconds or evidence.duration_seconds
            session.add(
                CompilationSegment(
                    compilation_id=compilation_uuid,
                    position=position,
                    clip_id=item.clip_id,
                    short_production_id=evidence.short_production_id,
                    short_publication_id=evidence.short_publication_id,
                    deterministic_score=Decimal(str(evidence.deterministic_score)),
                    opening_score=Decimal(str(evidence.opening_score)),
                    source_duration_seconds=Decimal(str(evidence.duration_seconds)),
                    source_start_seconds=Decimal(str(item.source_start_seconds)),
                    source_end_seconds=Decimal(str(end)),
                    transition_before=item.transition_before,
                    host_before=item.host_before,
                    host_after=item.host_after,
                    selection_reason=item.reason,
                    evidence=evidence.model_dump(mode="json"),
                    timing={},
                )
            )
        compilation.status = CompilationStatus.SCRIPTED.value
        compilation.stage = "plan_finalized"
        compilation.estimated_cost_usd = _compilation_cost(session, compilation_uuid)
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=compilation_id,
                event_type="compilation.plan_finalized",
                payload={
                    "compilation_id": compilation_id,
                    "segment_count": len(final.segments),
                    **provider,
                },
            )
        )
    return {
        "compilation_id": compilation_id,
        "segment_count": len(final.segments),
        **provider,
        "reused": False,
    }


def _safe_role(role: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", role).strip("_")[:42]


def _narration_parts(plan: FinalLongformPlan) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = [("opening", plan.opening_hook)]
    if plan.intro and plan.intro.strip():
        parts.append(("intro", plan.intro.strip()))
    for position, segment in enumerate(plan.segments):
        if segment.host_before and segment.host_before.strip():
            parts.append((f"seg:{position}:before", segment.host_before.strip()))
        if segment.host_after and segment.host_after.strip():
            parts.append((f"seg:{position}:after", segment.host_after.strip()))
    if plan.outro and plan.outro.strip():
        parts.append(("outro", plan.outro.strip()))
    return parts


def _persist_longform_tts(
    compilation_id: uuid.UUID,
    *,
    role: str,
    asset_key: str,
    metadata: dict[str, object],
) -> None:
    kind = f"narration_{_safe_role(role)}"
    with session_scope() as session:
        existing = session.scalar(
            select(CompilationAsset).where(
                CompilationAsset.compilation_id == compilation_id,
                CompilationAsset.kind == kind,
                CompilationAsset.generation == 1,
            )
        )
        if existing is not None:
            return
        session.add(
            CompilationAsset(
                compilation_id=compilation_id,
                kind=kind,
                generation=1,
                storage_key=asset_key,
                content_type="audio/wav",
                provider=str(metadata["provider"]),
                model=str(metadata["model"]),
                asset_metadata=metadata,
            )
        )
        session.add(
            UsageEvent(
                task=AITask.TTS.value,
                provider=str(metadata["provider"]),
                model=str(metadata["model"]),
                input_units=int(metadata["input_units"]),
                output_units=int(metadata["output_units"]),
                cost_usd=Decimal(str(metadata["cost_usd"])),
                reference_type="compilation",
                reference_id=str(compilation_id),
                usage_metadata={
                    "role": role,
                    "voice_profile": metadata["voice_profile"],
                    **dict(metadata.get("cost_metadata") or {}),
                },
            )
        )


@activity.defn
def generate_longform_narration_activity(compilation_id: str) -> dict[str, object]:
    compilation_uuid = uuid.UUID(compilation_id)
    settings = get_settings()
    store = ObjectStore(settings)
    store.ensure_bucket()

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if not compilation.final_plan:
            raise RuntimeError("final plan is required before long-form narration")
        plan = FinalLongformPlan.model_validate(compilation.final_plan)
        profile = (
            get_voice_profile(compilation.selected_voice_profile)
            if compilation.selected_voice_profile
            else choose_voice_profile(settings)
        )
        compilation.selected_voice_profile = profile.key
        compilation.status = CompilationStatus.VOICING.value
        compilation.error = None

    generated = 0
    reused = 0
    for role, text in _narration_parts(plan):
        safe = _safe_role(role)
        kind = f"narration_{safe}"
        audio_key = store.compilation_key(compilation_id, f"audio/{safe}-g1.wav")
        sidecar_key = store.compilation_key(compilation_id, f"audio/{safe}-g1.json")
        call_stage = f"tts_call_started_{safe}"
        with session_scope() as session:
            existing = session.scalar(
                select(CompilationAsset).where(
                    CompilationAsset.compilation_id == compilation_uuid,
                    CompilationAsset.kind == kind,
                    CompilationAsset.generation == 1,
                )
            )
            if existing is not None:
                reused += 1
                continue
            compilation = session.get(Compilation, compilation_uuid)
            if compilation is None:
                raise RuntimeError("compilation disappeared before TTS")
            if compilation.stage == call_stage:
                if store.exists(audio_key) and store.exists(sidecar_key):
                    metadata = json.loads(store.get_bytes(sidecar_key).decode("utf-8"))
                    _persist_longform_tts(
                        compilation_uuid,
                        role=role,
                        asset_key=audio_key,
                        metadata=metadata,
                    )
                    reused += 1
                    continue
                raise AmbiguousLongformPaidCall(
                    f"TTS call for {role} may already have been accepted; regenerate the voice "
                    "generation instead of automatically retrying"
                )
            compilation.stage = call_stage

        result = synthesize_speech(text, profile=profile, settings=settings)
        metadata: dict[str, object] = {
            "role": role,
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
        _persist_longform_tts(
            compilation_uuid,
            role=role,
            asset_key=audio_key,
            metadata=metadata,
        )
        generated += 1

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise RuntimeError("compilation disappeared after TTS")
        compilation.status = CompilationStatus.VOICED.value
        compilation.stage = "voice_ready"
        compilation.estimated_cost_usd = _compilation_cost(session, compilation_uuid)
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=compilation_id,
                event_type="compilation.voice_ready",
                payload={
                    "compilation_id": compilation_id,
                    "voice_profile": compilation.selected_voice_profile,
                    "generated_parts": generated,
                    "reused_parts": reused,
                },
            )
        )
    return {
        "compilation_id": compilation_id,
        "voice_profile": profile.key,
        "generated_parts": generated,
        "reused_parts": reused,
    }


@activity.defn
def build_longform_manifest_activity(compilation_id: str) -> dict[str, object]:
    compilation_uuid = uuid.UUID(compilation_id)
    settings = get_settings()
    store = ObjectStore(settings)
    store.ensure_bucket()

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        if not compilation.final_plan:
            raise RuntimeError("final plan is required before manifest construction")
        plan = FinalLongformPlan.model_validate(compilation.final_plan)
        segments = list(
            session.scalars(
                select(CompilationSegment)
                .where(CompilationSegment.compilation_id == compilation_uuid)
                .order_by(CompilationSegment.position)
            )
        )
        if len(segments) != len(plan.segments):
            raise RuntimeError("persisted segment count does not match final plan")
        clip_ids = [segment.clip_id for segment in segments]
        clips = {
            clip.id: clip
            for clip in session.scalars(select(Clip).where(Clip.id.in_(clip_ids)))
        }
        assets = list(
            session.scalars(
                select(CompilationAsset).where(
                    CompilationAsset.compilation_id == compilation_uuid,
                    CompilationAsset.kind.like("narration_%"),
                )
            )
        )
        narration_assets = {
            str((asset.asset_metadata or {}).get("role")): {
                "storage_key": asset.storage_key,
                "duration_seconds": float(
                    (asset.asset_metadata or {}).get("duration_seconds") or 0
                ),
            }
            for asset in assets
            if (asset.asset_metadata or {}).get("role")
        }
        segment_payload: list[dict[str, object]] = []
        for segment in segments:
            clip = clips.get(segment.clip_id)
            if clip is None:
                raise RuntimeError(f"compilation clip disappeared: {segment.clip_id}")
            segment_payload.append(
                {
                    "clip_id": str(segment.clip_id),
                    "source_key": clip.storage_key,
                    "source_duration_seconds": float(segment.source_duration_seconds),
                    "source_width": clip.width,
                    "source_height": clip.height,
                    "source_start_seconds": float(segment.source_start_seconds),
                    "source_end_seconds": float(
                        segment.source_end_seconds or segment.source_duration_seconds
                    ),
                    "transition_before": segment.transition_before,
                    "host_before": segment.host_before,
                    "host_after": segment.host_after,
                }
            )
        output_key = store.compilation_key(compilation_id, "render/longform-g1.mp4")
        manifest = build_longform_manifest(
            compilation_id=compilation_id,
            title_angle=plan.title_angle,
            opening_hook=plan.opening_hook,
            intro=plan.intro,
            outro=plan.outro,
            segments=segment_payload,
            narration_assets=narration_assets,
            source_audio_volume=settings.source_audio_volume,
            width=settings.longform_render_width,
            height=settings.longform_render_height,
            fps=settings.longform_render_fps,
            output_key=output_key,
        )

    manifest_key = store.compilation_key(compilation_id, "render/manifest.json")
    store.put_bytes(
        manifest.model_dump_json(indent=2).encode("utf-8"),
        manifest_key,
        content_type="application/json",
    )
    captions = [
        cue.model_dump(mode="json")
        for item in manifest.timeline
        if item.narration is not None
        for cue in item.narration.cues
    ]
    captions_key = store.compilation_key(compilation_id, "render/captions.json")
    store.put_bytes(
        json.dumps(captions, indent=2).encode("utf-8"),
        captions_key,
        content_type="application/json",
    )

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise RuntimeError("compilation disappeared while persisting manifest")
        compilation.render_manifest = manifest.model_dump(mode="json")
        compilation.status = CompilationStatus.RENDERING.value
        compilation.stage = "manifest_ready"
        for kind, key in (("manifest", manifest_key), ("captions", captions_key)):
            existing = session.scalar(
                select(CompilationAsset).where(
                    CompilationAsset.compilation_id == compilation_uuid,
                    CompilationAsset.kind == kind,
                    CompilationAsset.generation == 1,
                )
            )
            if existing is None:
                session.add(
                    CompilationAsset(
                        compilation_id=compilation_uuid,
                        kind=kind,
                        generation=1,
                        storage_key=key,
                        content_type="application/json",
                        asset_metadata={"manifest_version": manifest.version},
                    )
                )
        segment_by_clip = {
            str(segment.clip_id): segment
            for segment in session.scalars(
                select(CompilationSegment).where(
                    CompilationSegment.compilation_id == compilation_uuid
                )
            )
        }
        for item in manifest.timeline:
            if item.clip is None:
                continue
            segment = segment_by_clip.get(item.clip.clip_id)
            if segment is not None:
                segment.timing = {
                    "output_start_seconds": item.start_seconds,
                    "output_end_seconds": item.start_seconds + item.duration_seconds,
                    "duration_seconds": item.duration_seconds,
                }
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=compilation_id,
                event_type="compilation.render_manifest_ready",
                payload={
                    "compilation_id": compilation_id,
                    "manifest_key": manifest_key,
                    "output_duration_seconds": manifest.output_duration_seconds,
                },
            )
        )
    return {
        "compilation_id": compilation_id,
        "manifest_key": manifest_key,
        "output_key": manifest.output_key,
        "duration_seconds": manifest.output_duration_seconds,
    }


@activity.defn
def render_longform_activity(compilation_id: str) -> dict[str, object]:
    compilation_uuid = uuid.UUID(compilation_id)
    settings = get_settings()
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise ValueError(f"compilation not found: {compilation_id}")
        existing = session.scalar(
            select(CompilationAsset).where(
                CompilationAsset.compilation_id == compilation_uuid,
                CompilationAsset.kind == "render",
                CompilationAsset.generation == 1,
            )
        )
        if existing is not None:
            return {
                "compilation_id": compilation_id,
                "output_key": existing.storage_key,
                "reused": True,
            }
        if not compilation.render_manifest:
            raise RuntimeError("long-form render manifest is missing")
        compilation.stage = "rendering"
        manifest = LongformRenderManifest.model_validate(compilation.render_manifest)

    result = render_longform(manifest, settings=settings)

    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            raise RuntimeError("compilation disappeared after render")
        existing = session.scalar(
            select(CompilationAsset).where(
                CompilationAsset.compilation_id == compilation_uuid,
                CompilationAsset.kind == "render",
                CompilationAsset.generation == 1,
            )
        )
        if existing is None:
            session.add(
                CompilationAsset(
                    compilation_id=compilation_uuid,
                    kind="render",
                    generation=1,
                    storage_key=result.output_key,
                    content_type="video/mp4",
                    provider="remotion",
                    asset_metadata={
                        "duration_seconds": result.duration_seconds,
                        **result.metadata,
                    },
                )
            )
        compilation.status = CompilationStatus.REVIEW.value
        compilation.stage = "review"
        compilation.error = None
        compilation.estimated_cost_usd = _compilation_cost(session, compilation_uuid)
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=compilation_id,
                event_type="compilation.review_ready",
                payload={
                    "compilation_id": compilation_id,
                    "output_key": result.output_key,
                    "duration_seconds": result.duration_seconds,
                },
            )
        )
    return {
        "compilation_id": compilation_id,
        "output_key": result.output_key,
        "duration_seconds": result.duration_seconds,
        "reused": False,
    }


@activity.defn
def mark_compilation_failed(compilation_id: str, message: str) -> None:
    compilation_uuid = uuid.UUID(compilation_id)
    with session_scope() as session:
        compilation = session.get(Compilation, compilation_uuid)
        if compilation is None:
            return
        if compilation.status not in {
            CompilationStatus.APPROVED.value,
            CompilationStatus.REJECTED.value,
        }:
            compilation.status = CompilationStatus.FAILED.value
            compilation.stage = "failed"
        compilation.error = message[:8000]
        compilation.estimated_cost_usd = _compilation_cost(session, compilation_uuid)
        session.add(
            DomainEvent(
                aggregate_type="compilation",
                aggregate_id=compilation_id,
                event_type="compilation.failed",
                payload={"compilation_id": compilation_id, "error": compilation.error},
            )
        )
