"""Synthetic speech/fault injection; never calls a live speech provider."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select
from test_editorial_assets import scripted_project
from test_editorial_narration import wav
from test_editorial_projects import root
from test_editorial_projects import saved as _saved

from katcha import db
from katcha.ai import router
from katcha.audio import tts
from katcha.config import Settings
from katcha.domain import AITask
from katcha.editorial import generated_narration as generated
from katcha.editorial import narration
from katcha.editorial.run_schemas import StartEditorialRun
from katcha.editorial_models import EditorialNarration, EditorialProject
from katcha.intelligence_models import AIBudgetReservation, ChannelProfile
from katcha.models import UsageEvent
from katcha.provider_setting_models import ChannelProviderSetting
from katcha.services.channel_economics import _actual_spend_breakdown, active_reserved_cost
from katcha.services.editorial_runs import control_run, get_run, start_run

saved = _saved


@pytest.fixture
def speech(saved, monkeypatch):
    channel, project = scripted_project(saved)
    settings = Settings(
        _env_file=None,
        ai_enabled=True,
        ai_execution_mode="live",
        elevenlabs_api_key="synthetic-test",
        elevenlabs_usd_per_1000_credits=0.1,
    )
    monkeypatch.setattr(generated, "get_settings", lambda: settings)
    profile = tts.VoiceProfile(
        "channel", "1", "elevenlabs", "eleven_multilingual_v2", "voice-one", ""
    )
    monkeypatch.setattr(generated, "choose_voice_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(generated, "assert_ai_budget", lambda *_: None)
    objects = {}
    store = Mock()
    store.put_bytes.side_effect = lambda audio, key, **kwargs: objects.update({key: audio})
    store.get_bytes.side_effect = lambda key, **kwargs: objects[key]
    monkeypatch.setattr(generated, "ObjectStore", lambda: store)
    monkeypatch.setattr(narration, "ObjectStore", lambda: store)

    def reserve(task, channel_id, **kwargs):
        previous = generated._reservation(channel_id, kwargs["reservation_key"])
        with db.session_scope() as session:
            if previous:
                row = session.get(AIBudgetReservation, previous.id)
                assert row.status in {"reserved", "released"}
                row.status = "reserved"
                row.expires_at = datetime.now(UTC) + timedelta(minutes=30)
            else:
                row = AIBudgetReservation(
                    channel_profile_id=channel_id,
                    reservation_key=kwargs["reservation_key"],
                    task=task.value,
                    reference_type=kwargs["reference_type"],
                    reference_id=kwargs["reference_id"],
                    estimated_cost_usd=kwargs["estimated_increment_usd"],
                    status="reserved",
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    reservation_metadata={},
                )
                session.add(row)
            session.flush()
            return SimpleNamespace(reservation_id=row.id)

    monkeypatch.setattr(generated, "route_for_channel", reserve)
    provider = Mock(
        return_value=tts.TTSResult(
            audio=wav(),
            content_type="audio/wav",
            extension="wav",
            duration_seconds=2,
            target=router.ModelTarget("elevenlabs", profile.model),
            profile=profile,
            input_units=10,
            output_units=2000,
            estimated_cost_usd=Decimal("0.001"),
            cost_metadata={"request_id": "synthetic-request"},
        )
    )
    monkeypatch.setattr(generated, "_elevenlabs_tts", provider)
    run = start_run(
        channel,
        project,
        StartEditorialRun(
            expected_revision=1,
            idempotency_key="speech",
            target="narration",
            confirm_narration=True,
        ),
        actor="test",
    )
    return SimpleNamespace(
        run=run, settings=settings, provider=provider, objects=objects, store=store, profile=profile
    )


def resume(speech):
    row = get_run(speech.run.channel_profile_id, speech.run.project_id, speech.run.id)
    return control_run(
        row.channel_profile_id, row.project_id, row.id, expected_attempt=row.attempt, cancel=False
    )


def test_generation_attaches_measured_recording_and_accounts_channel_cost(speech):
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "completed"
    with db.session_scope() as session:
        recording = session.scalar(select(EditorialNarration))
        assert recording.sample_frames == 44101
        assert recording.sample_rate == 22050
        reservation = session.scalar(select(AIBudgetReservation))
        assert reservation.status == "settled"
        assert reservation.reservation_metadata["speech_result"]["sha256"] == recording.sha256
        profile = session.get(ChannelProfile, speech.run.channel_profile_id)
        total, _, _ = _actual_spend_breakdown(
            session, profile=profile, since=datetime(2020, 1, 1, tzinfo=UTC), publications=[]
        )
        assert total == Decimal("0.001")
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "stopped"
    speech.provider.assert_called_once()


def test_attachment_failure_recovers_audio_without_second_charge(speech, monkeypatch):
    real = generated.import_narration
    monkeypatch.setattr(
        generated, "import_narration", Mock(side_effect=RuntimeError("database interrupted"))
    )
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    monkeypatch.setattr(generated, "import_narration", real)
    monkeypatch.setattr(
        generated, "choose_voice_profile", Mock(side_effect=AssertionError("voice drift"))
    )
    row = resume(speech)
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "completed"
    speech.provider.assert_called_once()
    with db.session_scope() as session:
        assert len(list(session.scalars(select(UsageEvent)))) == 1


def test_settlement_failure_recovers_exact_audio_and_settles_once(speech, monkeypatch):
    real = generated.record_usage
    monkeypatch.setattr(
        generated, "record_usage", Mock(side_effect=RuntimeError("settlement interrupted"))
    )
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    monkeypatch.setattr(generated, "record_usage", real)
    row = resume(speech)
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "completed"
    speech.provider.assert_called_once()


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("quota exceeded but response lost"), RuntimeError("malformed audio")],
)
def test_unknown_outcome_keeps_budget_and_prevents_repeat(speech, failure):
    speech.provider.side_effect = failure
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    with db.session_scope() as session:
        row = session.scalar(select(AIBudgetReservation))
        row.expires_at = datetime.now(UTC) - timedelta(days=3)
    row = resume(speech)
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "blocked"
    speech.provider.assert_called_once()
    with db.session_scope() as session:
        reservation = session.scalar(select(AIBudgetReservation))
        router._expire_reservations(session, row.channel_profile_id, datetime.now(UTC))
        assert reservation.status == "dispatched"
        assert active_reserved_cost(session, row.channel_profile_id) > 0


def test_explicit_rejection_releases_reservation_and_can_resume(speech):
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.com"))
    speech.provider.side_effect = [
        httpx.HTTPStatusError("rate limit", request=response.request, response=response),
        speech.provider.return_value,
    ]
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    with db.session_scope() as session:
        assert session.scalar(select(AIBudgetReservation)).status == "released"
    row = resume(speech)
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "completed"
    assert speech.provider.call_count == 2


def test_storage_failure_blocks_regeneration(speech):
    speech.store.put_bytes.side_effect = RuntimeError("object storage interrupted")
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    row = resume(speech)
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "blocked"
    speech.provider.assert_called_once()


@pytest.mark.parametrize("blocker", ["voice", "revision", "rate", "fixture", "cap"])
def test_generation_preconditions_spend_nothing(speech, blocker):
    if blocker in {"voice", "revision", "cap"}:
        with db.session_scope() as session:
            if blocker == "voice":
                session.add(
                    ChannelProviderSetting(
                        channel_profile_id=speech.run.channel_profile_id,
                        provider="elevenlabs",
                        enabled=False,
                        config={},
                    )
                )
            elif blocker == "revision":
                session.get(EditorialProject, speech.run.project_id).revision += 1
            else:
                from katcha.editorial_models import EditorialRun

                row = session.get(EditorialRun, speech.run.id)
                row.options = {**row.options, "max_narration_estimate_usd": 0.000001}
    elif blocker == "rate":
        speech.settings.elevenlabs_usd_per_1000_credits = 0
    else:
        speech.settings.ai_execution_mode = "fixture"
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    speech.provider.assert_not_called()


def test_dispatch_claim_is_atomic_and_settlement_replays(speech):
    frozen = {
        "profile": asdict(speech.profile),
        "output_format": "pcm_24000",
        "usd_per_1000_credits": "0.1",
    }
    from katcha.editorial.project_schemas import EditorialDraft

    beat = EditorialDraft.model_validate(speech.run.artifacts["input_draft"]).script[0]
    generated.generate_beat(speech.run, beat, frozen, speech.settings)
    with db.session_scope() as session:
        identity = session.scalar(select(AIBudgetReservation.id))
    with pytest.raises(router.BudgetExceeded):
        router.dispatch_budget_reservation(identity)
    assert generated.generate_beat(speech.run, beat, frozen, speech.settings) == wav()
    speech.provider.assert_called_once()


def test_api_requires_generation_confirmation_and_write_scope(saved):
    client, channel, _ = saved
    _, project = scripted_project(saved)
    url = f"{root(channel)}/{project}/runs"
    payload = {"target": "narration", "expected_revision": 1, "idempotency_key": "generation"}
    assert client.post(url, json=payload).status_code == 422
    payload["confirm_narration"] = True
    assert (
        client.post(
            url, json=payload, headers={"Authorization": "Bearer editorial-reader-token-00001"}
        ).status_code
        == 403
    )


def test_dispatched_reservation_blocks_router_and_global_budget(speech, monkeypatch):
    speech.provider.side_effect = httpx.ReadTimeout("unknown")
    generated.generate_narration(str(speech.run.id), 1)
    with db.session_scope() as session:
        row = session.scalar(select(AIBudgetReservation))
        key = row.reservation_key
        row.estimated_cost_usd = Decimal("10")
    monkeypatch.setattr(router, "_available_providers", lambda *_: {"elevenlabs"})
    monkeypatch.setattr(router, "get_settings", lambda: speech.settings)
    with pytest.raises(router.BudgetExceeded, match="may already have been accepted"):
        router.route_for_channel(
            AITask.TTS,
            speech.run.channel_profile_id,
            reservation_key=key,
            estimated_increment_usd=Decimal("0.1"),
            preferred_target=router.ModelTarget("elevenlabs", speech.profile.model),
        )
    speech.settings.ai_budget_usd_monthly = 1
    with pytest.raises(router.BudgetExceeded, match="monthly budget exceeded"):
        router.assert_ai_budget(Decimal("0"))


def test_shared_tts_timeout_keeps_reservation_and_never_repeats(speech, monkeypatch):
    decision = generated.route_for_channel(
        AITask.TTS,
        speech.run.channel_profile_id,
        reservation_key="shared-tts-test",
        estimated_increment_usd=Decimal("0.05"),
        reference_type="production",
        reference_id="test",
    )
    monkeypatch.setattr(tts, "route_for_channel", lambda *args, **kwargs: decision)
    monkeypatch.setattr(tts, "assert_ai_budget", lambda *_: None)
    provider = Mock(side_effect=httpx.ReadTimeout("response interrupted"))
    monkeypatch.setattr(tts, "_openai_tts", provider)
    profile = tts.get_voice_profile("openai_youth_v2")
    with pytest.raises(httpx.ReadTimeout):
        tts.synthesize_speech(
            "A saved line",
            profile=profile,
            settings=speech.settings,
            channel_profile_id=speech.run.channel_profile_id,
            reservation_key="shared-tts-test",
        )
    with pytest.raises(router.BudgetExceeded):
        tts.synthesize_speech(
            "A saved line",
            profile=profile,
            settings=speech.settings,
            channel_profile_id=speech.run.channel_profile_id,
            reservation_key="shared-tts-test",
        )
    provider.assert_called_once()


def test_stored_audio_corruption_never_regenerates(speech, monkeypatch):
    monkeypatch.setattr(
        generated, "import_narration", Mock(side_effect=RuntimeError("interrupted"))
    )
    generated.generate_narration(str(speech.run.id), 1)
    for key in speech.objects:
        speech.objects[key] = b"changed"
    row = resume(speech)
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "blocked"
    speech.provider.assert_called_once()


def billing_body(**changes):
    from katcha.editorial.run_schemas import ReconcileNarrationBilling

    return ReconcileNarrationBilling.model_validate(
        {
            "idempotency_key": "receipt-one",
            "beat_id": "beat-1",
            "expected_dispatch_count": 1,
            "outcome": "not_charged",
            "actual_cost_usd": 0,
            "provider_receipt": "Operator verified provider request abc was rejected",
            "confirmed": True,
            **changes,
        }
    )


def reconcile(speech, body):
    return generated.reconcile_billing(
        speech.run.channel_profile_id, speech.run.project_id, speech.run.id, body, actor="test"
    )


def test_verified_no_charge_replays_without_releasing_newer_attempt(speech):
    speech.provider.side_effect = httpx.ReadTimeout("unknown")
    generated.generate_narration(str(speech.run.id), 1)
    first = reconcile(speech, billing_body())
    assert reconcile(speech, billing_body()) == first
    row = resume(speech)
    generated.generate_narration(str(row.id), row.attempt)
    assert speech.provider.call_count == 2
    assert reconcile(speech, billing_body()) == first
    with db.session_scope() as session:
        reservation = session.scalar(select(AIBudgetReservation))
        assert reservation.status == "dispatched"
        assert reservation.reservation_metadata["dispatch_count"] == 2
    with pytest.raises(ValueError, match="attempt changed"):
        reconcile(speech, billing_body(idempotency_key="new-receipt"))
    with pytest.raises(ValueError, match="different details"):
        reconcile(speech, billing_body(provider_receipt="Different evidence"))


def test_verified_charge_is_counted_once_and_cannot_regenerate(speech):
    speech.provider.side_effect = httpx.ReadTimeout("unknown")
    generated.generate_narration(str(speech.run.id), 1)
    body = billing_body(outcome="charged", actual_cost_usd=0.13)
    reconcile(speech, body)
    reconcile(speech, body)
    with db.session_scope() as session:
        events = list(session.scalars(select(UsageEvent)))
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.13")
        assert active_reserved_cost(session, speech.run.channel_profile_id) == 0
    row = resume(speech)
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "blocked"
    speech.provider.assert_called_once()


def test_reconciliation_scopes_confirmation_and_running_work(speech, saved):
    client, channel, other = saved
    speech.provider.side_effect = httpx.ReadTimeout("unknown")
    generated.generate_narration(str(speech.run.id), 1)
    url = f"{root(channel)}/{speech.run.project_id}/runs/{speech.run.id}/narration-billing"
    payload = billing_body().model_dump(mode="json")
    assert client.post(url, json={**payload, "confirmed": False}).status_code == 422
    assert (
        client.post(
            url, json=payload, headers={"Authorization": "Bearer editorial-reader-token-00001"}
        ).status_code
        == 403
    )
    assert client.post(url.replace(str(channel), str(other)), json=payload).status_code == 403
    with pytest.raises(ValueError, match="not found"):
        generated.reconcile_billing(
            other, speech.run.project_id, speech.run.id, billing_body(), actor="test"
        )
    resumed = resume(speech)
    assert client.post(url, json=payload).status_code == 409
    control_run(
        resumed.channel_profile_id,
        resumed.project_id,
        resumed.id,
        expected_attempt=resumed.attempt,
        cancel=True,
    )
    assert client.post(url, json=payload).status_code == 201
    assert client.post(url, json=payload).status_code == 201


@pytest.mark.parametrize("rejected", [False, True])
def test_late_provider_result_cannot_touch_newer_dispatch(speech, rejected):
    def late(*args):
        with db.session_scope() as session:
            reservation = session.scalar(select(AIBudgetReservation))
            # Simulate reconciliation followed by a new reservation dispatch while
            # the first provider request is still finishing.
            reservation.reservation_metadata = {
                **reservation.reservation_metadata,
                "dispatch_count": 2,
            }
        if rejected:
            response = httpx.Response(429, request=httpx.Request("POST", "https://example.com"))
            raise httpx.HTTPStatusError("rejected", request=response.request, response=response)
        return speech.provider.return_value

    speech.provider.side_effect = late
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    with db.session_scope() as session:
        reservation = session.scalar(select(AIBudgetReservation))
        assert reservation.status == "dispatched"
        assert "speech_result" not in reservation.reservation_metadata
        assert session.scalar(select(UsageEvent)) is None


def test_resume_reuses_completed_beats(speech):
    from katcha.editorial_models import EditorialRun

    with db.session_scope() as session:
        run = session.get(EditorialRun, speech.run.id)
        script = run.artifacts["input_draft"]["script"]
        run.artifacts = {
            **run.artifacts,
            "input_draft": {
                **run.artifacts["input_draft"],
                "script": [*script, {**script[0], "id": "beat-2"}],
            },
        }
        # Seed the saved revision consistently with the synthetic two-beat script.
        from katcha.editorial_models import EditorialRevision

        revision = session.get(EditorialRevision, (run.project_id, 1))
        revision.draft = run.artifacts["input_draft"]
    speech.provider.side_effect = [
        speech.provider.return_value,
        httpx.ReadTimeout("second beat unknown"),
    ]
    assert generated.generate_narration(str(speech.run.id), 1)["status"] == "blocked"
    reconcile(speech, billing_body(beat_id="beat-2"))
    row = resume(speech)
    speech.provider.side_effect = None
    assert generated.generate_narration(str(row.id), row.attempt)["status"] == "completed"
    assert speech.provider.call_count == 3
    with db.session_scope() as session:
        assert len(list(session.scalars(select(EditorialNarration)))) == 2
        assert len(list(session.scalars(select(UsageEvent)))) == 2


def test_generation_activity_skips_intake_and_is_registered(monkeypatch):
    import asyncio

    from katcha.orchestration import editorial_activities, editorial_workflows

    calls = []

    async def execute(name, *args, **kwargs):
        calls.append((name, kwargs))
        return {"target": "narration"} if name == "editorial_begin" else {"status": "completed"}

    monkeypatch.setattr(editorial_workflows.workflow, "execute_activity", execute)
    result = asyncio.run(editorial_workflows.EditorialProjectWorkflow().run("fixture", 1))
    assert result["status"] == "completed"
    assert [name for name, _ in calls] == ["editorial_begin", "editorial_generate_narration"]
    assert calls[-1][1]["retry_policy"].maximum_attempts == 1
    assert (
        editorial_activities.editorial_generate_narration
        in editorial_activities.EDITORIAL_ACTIVITIES
    )
