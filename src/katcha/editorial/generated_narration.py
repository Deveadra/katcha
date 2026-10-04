"""Recoverable per-beat speech. Unknown provider outcomes never trigger a second call."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update

from katcha.ai.router import (
    ModelTarget,
    assert_ai_budget,
    dispatch_budget_reservation,
    record_usage,
    release_budget_reservation,
    route_for_channel,
)
from katcha.audio.tts import (
    VoiceProfile,
    _elevenlabs_tts,
    choose_voice_profile,
    speech_request_rejected,
)
from katcha.config import get_settings
from katcha.db import session_scope
from katcha.domain import AITask
from katcha.editorial.narration import MAX_AUDIO_BYTES, import_narration, measure_wav, voice_enabled
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial_models import EditorialNarration, EditorialProject, EditorialRun
from katcha.integrations.storage import ObjectStore
from katcha.intelligence_models import AIBudgetReservation, ChannelStrategyVersion
from katcha.models import DomainEvent, UsageEvent
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound, _digest
from katcha.services.editorial_runs import EditorialStopped, checkpoint


def _reservation(channel_id, key):
    with session_scope() as session:
        row = session.scalar(
            select(AIBudgetReservation).where(
                AIBudgetReservation.channel_profile_id == channel_id,
                AIBudgetReservation.reservation_key == key,
            )
        )
        if row is not None:
            session.expunge(row)
        return row


def _settle(row, receipt):
    record_usage(
        task=AITask.TTS,
        target=ModelTarget("elevenlabs", receipt["profile"]["model"]),
        input_units=receipt["input_units"],
        output_units=receipt["output_units"],
        cost_usd=Decimal(receipt["cost_usd"]),
        reference_type="editorial_project",
        reference_id=row.reference_id,
        reservation_id=row.id,
        metadata={
            **receipt["cost_metadata"],
            "voice_profile": receipt["profile"]["key"],
            "audio_sha256": receipt["sha256"],
        },
    )


def generate_beat(run, beat, frozen, settings):
    key = f"editorial-voice:{run.project_id}:{run.input_revision}:{beat.id}"
    digest = _digest({"text": beat.narration, "voice": frozen})
    previous = _reservation(run.channel_profile_id, key)
    if previous:
        metadata = previous.reservation_metadata or {}
        if metadata.get("speech_digest") not in {None, digest}:
            raise EditorialConflict(
                "A different voice request already exists for this beat revision"
            )
        receipt = metadata.get("speech_result")
        if receipt:
            audio = ObjectStore().get_bytes(receipt["audio_key"], max_bytes=MAX_AUDIO_BYTES)
            if hashlib.sha256(audio).hexdigest() != receipt["sha256"]:
                raise EditorialConflict("Saved narration checksum changed; recovery is blocked")
            measure_wav(audio)
            _settle(previous, receipt)
            return audio
        if previous.status in {"dispatched", "settled"}:
            raise EditorialConflict(
                "A speech request may have been charged but has no recoverable audio receipt. "
                "Automatic regeneration is blocked. Check provider history and upload recovered "
                "audio, or use a new script revision after reconciling the charge."
            )
    profile = VoiceProfile(**frozen["profile"])
    estimate = Decimal(len(beat.narration)) / 1000 * Decimal(frozen["usd_per_1000_credits"])
    assert_ai_budget(estimate)
    decision = route_for_channel(
        AITask.TTS,
        run.channel_profile_id,
        estimated_increment_usd=estimate,
        reference_type="editorial_project",
        reference_id=str(run.project_id),
        reservation_key=key,
        preferred_target=ModelTarget("elevenlabs", profile.model),
    )
    # Freeze the request before dispatch; concurrent attempts share one atomic claim.
    with session_scope() as session:
        session.execute(
            update(AIBudgetReservation)
            .where(AIBudgetReservation.id == decision.reservation_id)
            .values(status=AIBudgetReservation.status)
        )
        row = session.scalar(
            select(AIBudgetReservation)
            .where(AIBudgetReservation.id == decision.reservation_id)
            .with_for_update()
        )
        metadata = dict(row.reservation_metadata or {})
        if metadata.get("speech_digest") not in {None, digest}:
            raise EditorialConflict("Voice request changed while reserving its budget")
        row.reservation_metadata = {**metadata, "speech_digest": digest, "speech_voice": frozen}
    dispatch_count = dispatch_budget_reservation(decision.reservation_id)
    try:
        result = _elevenlabs_tts(beat.narration, profile, settings)
    except Exception as exc:
        if speech_request_rejected(exc):
            release_budget_reservation(
                decision.reservation_id,
                reason="speech_explicitly_rejected",
                expected_dispatch_count=dispatch_count,
            )
        raise
    measure_wav(result.audio)
    sha = hashlib.sha256(result.audio).hexdigest()
    storage_key = f"editorial/{run.project_id}/generated/{decision.reservation_id}/{sha}.wav"
    ObjectStore().put_bytes(result.audio, storage_key, content_type="audio/wav")
    receipt = {
        "audio_key": storage_key,
        "sha256": sha,
        "profile": asdict(result.profile),
        "input_units": result.input_units,
        "output_units": result.output_units,
        "cost_usd": str(result.estimated_cost_usd),
        "cost_metadata": result.cost_metadata,
    }
    with session_scope() as session:
        session.execute(
            update(AIBudgetReservation)
            .where(AIBudgetReservation.id == decision.reservation_id)
            .values(status=AIBudgetReservation.status)
        )
        row = session.scalar(
            select(AIBudgetReservation)
            .where(AIBudgetReservation.id == decision.reservation_id)
            .with_for_update()
        )
        if (
            row.status != "dispatched"
            or row.reservation_metadata.get("dispatch_count") != dispatch_count
        ):
            raise EditorialConflict("Speech reservation changed before its receipt was saved")
        row.reservation_metadata = {**row.reservation_metadata, "speech_result": receipt}
    # If settlement or attachment fails, the next attempt recovers these exact bytes.
    _settle(_reservation(run.channel_profile_id, key), receipt)
    return result.audio


def generate_narration(run_id: str, attempt: int) -> dict:
    run = None
    try:
        run = checkpoint(run_id, attempt, stage="generating_narration")
        settings = get_settings()
        if not settings.ai_enabled or settings.resolved_ai_execution_mode() != "live":
            raise EditorialConflict(
                "Generated editorial narration requires a live voice connection"
            )
        draft = EditorialDraft.model_validate(run.artifacts["input_draft"])
        frozen = run.artifacts.get("narration_voice")
        if frozen is None:
            profile = choose_voice_profile(
                settings,
                target=ModelTarget("elevenlabs", settings.elevenlabs_model_id),
                channel_profile_id=run.channel_profile_id,
                voice_role="longform_primary",
            )
            rate = Decimal(str(settings.elevenlabs_usd_per_1000_credits))
            if rate <= 0 or not rate.is_finite():
                raise EditorialConflict(
                    "Configure the ElevenLabs credit cost before generating narration"
                )
            frozen = {
                "profile": asdict(profile),
                "output_format": settings.elevenlabs_output_format,
                "usd_per_1000_credits": str(rate),
            }
            run = checkpoint(run_id, attempt, artifacts={"narration_voice": frozen})
        estimate = (
            sum(len(beat.narration) for beat in draft.script)
            / Decimal(1000)
            * Decimal(frozen["usd_per_1000_credits"])
        )
        if estimate > Decimal(str(run.options["max_narration_estimate_usd"])):
            raise EditorialConflict(
                "Narration estimate exceeds this run's limit; increase it explicitly"
            )
        settings = settings.model_copy(
            update={
                "elevenlabs_output_format": frozen["output_format"],
                "elevenlabs_usd_per_1000_credits": float(frozen["usd_per_1000_credits"]),
            }
        )
        recordings = dict(run.artifacts.get("generated_narration") or {})
        for beat in draft.script:
            run = checkpoint(run_id, attempt, stage="generating_narration")
            with session_scope() as session:
                channel = ensure_active_profile(session, run.channel_profile_id)
                strategy = session.scalar(
                    select(ChannelStrategyVersion).where(
                        ChannelStrategyVersion.channel_profile_id == channel.id,
                        ChannelStrategyVersion.version == channel.active_strategy_version,
                    )
                )
                policy = strategy.routing_policy if strategy else {}
                if (
                    policy.get("external_provider_calls") is False
                    or policy.get("execution_mode") == "fixture"
                ):
                    raise EditorialConflict("Channel policy disables live external provider calls")
                project = session.get(EditorialProject, run.project_id)
                if project.revision != run.input_revision or not voice_enabled(
                    session, run.channel_profile_id
                ):
                    raise EditorialConflict(
                        "Script changed or channel voice was disabled; generation stopped"
                    )
                if beat.id in recordings:
                    recording = session.get(EditorialNarration, uuid.UUID(recordings[beat.id]))
                    if recording is None or recording.status != "active":
                        raise EditorialConflict(
                            "A saved generated recording was removed; "
                            "review recordings before resuming"
                        )
            if beat.id in recordings:
                continue
            audio = generate_beat(run, beat, frozen, settings)
            checkpoint(run_id, attempt)
            recording = import_narration(
                run.channel_profile_id,
                run.project_id,
                revision=run.input_revision,
                beat_id=beat.id,
                idempotency_key=f"generated:{run.input_revision}:{beat.id}",
                audio=audio,
                actor=run.actor,
                permitted_use=True,
            )
            if recording["status"] != "active":
                raise EditorialConflict(
                    "Generated recording was removed; it will not be restored automatically"
                )
            recordings[beat.id] = recording["id"]
            checkpoint(run_id, attempt, artifacts={"generated_narration": recordings})
        checkpoint(
            run_id,
            attempt,
            status="completed",
            stage="narration_ready_for_review",
            artifacts={"narration_billing": []},
        )
        return {"editorial_run_id": run_id, "status": "completed"}
    except EditorialStopped:
        return {"editorial_run_id": run_id, "status": "stopped"}
    except Exception as exc:
        try:
            checkpoint(
                run_id,
                attempt,
                artifacts={"narration_billing": billing_holds(run) if run else []},
                status="blocked",
                error=(
                    f"Narration stopped: {type(exc).__name__}: {str(exc)[:1000]}. "
                    "Saved recordings are retained. "
                    "Resume checks receipts before any provider call."
                ),
            )
        except EditorialStopped:
            return {"editorial_run_id": run_id, "status": "stopped"}
        return {"editorial_run_id": run_id, "status": "blocked"}


def billing_holds(run):
    prefix = f"editorial-voice:{run.project_id}:{run.input_revision}:"
    keys = {
        prefix + beat["id"]: beat["id"]
        for beat in run.artifacts.get("input_draft", {}).get("script", [])
    }
    with session_scope() as session:
        rows = session.scalars(
            select(AIBudgetReservation).where(
                AIBudgetReservation.channel_profile_id == run.channel_profile_id,
                AIBudgetReservation.reservation_key.in_(keys),
                AIBudgetReservation.status == "dispatched",
            )
        )
        return [
            {
                "beat_id": keys[row.reservation_key],
                "dispatch_count": row.reservation_metadata["dispatch_count"],
                "estimated_cost_usd": str(row.estimated_cost_usd),
            }
            for row in rows
            if not row.reservation_metadata.get("speech_result")
        ]


def reconcile_billing(channel_id, project_id, run_id, body, *, actor):
    """Explicit operator reconciliation, serialized with receipt storage and dispatch."""
    digest = _digest({**body.model_dump(mode="json"), "actor": actor})
    with session_scope() as session:
        ensure_active_profile(session, channel_id)
        session.execute(
            update(EditorialRun)
            .where(
                EditorialRun.id == run_id,
                EditorialRun.channel_profile_id == channel_id,
                EditorialRun.project_id == project_id,
            )
            .values(updated_at=EditorialRun.updated_at)
        )
        run = session.get(EditorialRun, run_id)
        if run is None or (run.channel_profile_id, run.project_id) != (channel_id, project_id):
            raise EditorialNotFound("Editorial run not found in this project")
        if run.options.get("target") != "narration":
            raise EditorialConflict("This run did not generate narration")
        key = f"editorial-voice:{project_id}:{run.input_revision}:{body.beat_id}"
        session.execute(
            update(AIBudgetReservation)
            .where(
                AIBudgetReservation.channel_profile_id == channel_id,
                AIBudgetReservation.reservation_key == key,
            )
            .values(status=AIBudgetReservation.status)
        )
        row = session.scalar(
            select(AIBudgetReservation)
            .where(
                AIBudgetReservation.channel_profile_id == channel_id,
                AIBudgetReservation.reservation_key == key,
            )
            .with_for_update()
        )
        if row is None:
            raise EditorialNotFound("Speech reservation not found for this beat")
        metadata = dict(row.reservation_metadata or {})
        receipts = dict(metadata.get("speech_reconciliations") or {})
        previous = receipts.get(body.idempotency_key)
        if previous:
            if previous["digest"] != digest:
                raise EditorialConflict("Reconciliation identity was used for different details")
            return previous
        if run.status in {"queued", "running"}:
            raise EditorialConflict("Wait for speech generation to stop before reconciling billing")
        if row.status != "dispatched" or metadata.get("speech_result"):
            raise EditorialConflict(
                "This request has no unresolved charge without an audio receipt"
            )
        if metadata.get("dispatch_count") != body.expected_dispatch_count:
            raise EditorialConflict("Speech attempt changed. Reload before reconciling billing")
        row.status = "settled" if body.outcome == "charged" else "released"
        row.actual_cost_usd = Decimal(str(body.actual_cost_usd))
        row.settled_at = datetime.now(UTC)
        receipt = {
            "digest": digest,
            "outcome": body.outcome,
            "actual_cost_usd": str(body.actual_cost_usd),
            "actor": actor,
            "dispatch_count": body.expected_dispatch_count,
            "provider_receipt": body.provider_receipt,
            "beat_id": body.beat_id,
        }
        receipts[body.idempotency_key] = receipt
        row.reservation_metadata = {**metadata, "speech_reconciliations": receipts}
        if body.outcome == "charged":
            session.add(
                UsageEvent(
                    task=AITask.TTS.value,
                    provider="elevenlabs",
                    model=metadata.get("model", "unknown"),
                    input_units=0,
                    output_units=0,
                    cost_usd=row.actual_cost_usd,
                    reference_type="editorial_project",
                    reference_id=str(project_id),
                    usage_metadata={
                        "manual_billing_reconciliation": True,
                        "reservation_id": str(row.id),
                        "provider_receipt": body.provider_receipt,
                        "actor": actor,
                    },
                )
            )
        session.add(
            DomainEvent(
                aggregate_type="editorial_project",
                aggregate_id=str(project_id),
                event_type="editorial.narration_billing_reconciled",
                payload={**receipt, "channel_profile_id": str(channel_id), "run_id": str(run_id)},
            )
        )
        return receipt
