"""KIN-101 offline script-to-Bin proof; reference evidence never grants rights."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter

import httpx
import pytest
from katcha.editorial.media_preparation_schemas import StartMediaPreparation
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from katcha import db
from katcha.acquisition.adapters import DiscoveredCandidate, DiscoveryBatch, DiscoveryProviderError
from katcha.editorial.project_schemas import CreateEditorialProject, SaveEditorialDraft
from katcha.editorial.source_intelligence.contracts import ProviderPolicyV1
from katcha.editorial.source_intelligence.jobs import SearchJobLedger
from katcha.editorial.source_intelligence.planner import SearchCapability
from katcha.editorial.source_intelligence.query_expansion import ProviderRoute
from katcha.intelligence_models import ChannelProfile
from katcha.models import Clip, DomainEvent, SourceItem
from katcha.services import editorial_media_preparation as preparation
from katcha.services.editorial_media_bin import add_project_media_item, list_project_media_items
from katcha.services.editorial_projects import create_project, get_project, save_draft


class ReferenceProvider:
    """Public metadata pages with a duplicate canonical URL and a real cursor."""

    key = "reference_fixture"
    version = "v1"

    def __init__(self):
        self.calls = []

    def discover(self, query, cursor):
        self.calls.append((dict(query), dict(cursor)))
        if cursor:
            return DiscoveryBatch(items=())
        item = DiscoveredCandidate(
            "https://publisher.example/press/vision#provider-fragment",
            external_id="press-vision",
            title=query["q"],
            creator="Fixture publisher",
            metadata={
                "description": query["q"],
                "license": "production_cleared",
                "download_url": "https://publisher.example/private.mp4?token=secret",
            },
        )
        return DiscoveryBatch(items=(item, item), next_cursor={"page": 2}, done=False)


class UnavailableProvider:
    key = "unavailable_fixture"
    version = "v1"

    def __init__(self):
        self.calls = []

    def discover(self, query, cursor):
        self.calls.append((dict(query), dict(cursor)))
        raise DiscoveryProviderError(
            "sensitive upstream details", kind="forbidden", status_code=403
        )


@pytest.fixture
def script_to_bin(monkeypatch, tmp_path):
    """Real existing database/model/services, trusted fixture adapters, no network."""
    db.load_model_metadata()
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    channel, other_channel = uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        for channel_id in (channel, other_channel):
            session.add(
                ChannelProfile(
                    id=channel_id,
                    youtube_connection_id=uuid.uuid4(),
                    status="active",
                    timezone="UTC",
                    profile_metadata={},
                )
            )

    def reject_network(*args, **kwargs):
        pytest.fail("script-to-Bin integration must use fixture providers only")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)
    good, bad = ReferenceProvider(), UnavailableProvider()
    ledger_path = tmp_path / "preparation-ledger.sqlite"
    policy = ProviderPolicyV1(
        version="fixture-policy-v1",
        max_calls=48,
        max_results=480,
        external_search_authorized=True,
    )
    routes = tuple(
        ProviderRoute(
            SearchCapability(adapter.key, "v1", "fixture", 100, external=False),
            policy,
            "public_reference",
            max_results=10,
        )
        for adapter in (good, bad)
    )

    def runtime():
        # Reopening the ledger for every request proves durable receipt replay.
        return preparation.PreparationRuntime(
            ledger=SearchJobLedger(ledger_path),
            routes=routes,
            adapters={good.key: good, bad.key: bad},
            max_jobs=48,
            batch_size=5,
        )

    monkeypatch.setattr(preparation, "runtime", runtime)
    yield channel, other_channel, good, bad, ledger_path
    engine.dispose()


def imported_project(channel, text, *, owned_clip=False):
    source_url = "https://owned.example/vision.mp4"
    clip_id = None
    if owned_clip:
        clip_id = uuid.uuid4()
        with db.session_scope() as session:
            session.add(
                Clip(
                    id=clip_id, sha256="a" * 64, storage_key="canonical/vision.mp4", status="scored"
                )
            )
            session.add(
                SourceItem(
                    source_url=source_url,
                    canonical_url=source_url,
                    platform="web",
                    clip_id=clip_id,
                    source_metadata={"channel_profile_id": str(channel)},
                )
            )
    project = create_project(
        channel,
        CreateEditorialProject.model_validate(
            {
                "idempotency_key": "project-" + uuid.uuid4().hex,
                "brief": {
                    "prompt": "Prepare the supplied script without rewriting it.",
                    "source_urls": [source_url],
                    "source_clip_bindings": {source_url: str(clip_id)} if clip_id else {},
                    "script_seed": {
                        "text": text,
                        "origin": "operator_paste",
                        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    },
                },
            }
        ),
        actor="fixture-editor",
    )
    return project, clip_id


def start(channel, project, *, key="prepare", revision=0, authorized=True):
    return preparation.start_preparation(
        channel,
        project.id,
        StartMediaPreparation(
            idempotency_key=key,
            expected_revision=revision,
            external_search_authorized=authorized,
        ),
        actor="fixture-editor",
    )


def finish(channel, project_id, run_id):
    for _ in range(60):
        row = preparation.get_preparation(channel, project_id, run_id)
        if preparation.response(row)["status"] == "completed":
            return preparation.response(row)
        preparation.resume_preparation(channel, project_id, run_id)
    pytest.fail("bounded fixture preparation did not terminate")


def test_full_script_to_bin_preserves_requirements_provenance_gaps_and_owned_media(script_to_bin):
    channel, _, good, bad, _ = script_to_bin
    # Exceeds one planning/dispatch batch and the finite 48-job planning budget.
    text = (
        "  Opening narration with exact Unicode café.\n"
        + "\n".join(
            f"[VISUAL: Vision window evidence scene {index:02d}]\nNarration for scene {index:02d}."
            for index in range(36)
        )
        + "\nClosing narration.  "
    )
    project, clip_id = imported_project(channel, text, owned_clip=True)
    run = start(channel, project)
    pending = preparation.response(run)
    assert pending["status"] == "pending"
    assert not good.calls and not bad.calls
    assert get_project(channel, project.id).brief["script_seed"]["text"] == text
    manifest = pending["manifest"]
    assert manifest["plan"]["source_text"] == text
    assert len(manifest["requirements"]) > 36
    requirement_ids = {item["requirement_id"] for item in manifest["requirements"]}

    operator_item = add_project_media_item(
        channel,
        project.id,
        run.input_revision,
        request_key="operator-selection",
        actor="fixture-editor",
        kind="citation",
        external_reference_url="https://publisher.example/press/operator-selection",
        requirement_ids=[sorted(requirement_ids)[0]],
        status="review_required",
        rights_status="restricted",
        notes="Keep my manually selected scene and rights review note.",
        provenance={"basis": "operator_selection"},
    )
    preparation.resume_preparation(channel, project.id, run.id)
    partial = preparation.response(preparation.get_preparation(channel, project.id, run.id))
    assert partial["status"] in {"pending", "running"}
    assert 0 < len(good.calls) + len(bad.calls) <= 5
    completed = finish(channel, project.id, run.id)
    assert completed["status"] == "completed" and not completed["stale"]
    assert preparation.latest_preparation(channel, project.id).id == run.id
    coverage = completed["coverage"]
    assert {item["requirement_id"] for item in coverage["requirements"]} == requirement_ids
    assert coverage["usable_media_count"] == 0
    assert any(item["gaps"] for item in coverage["requirements"])
    assert completed["planning_gaps"]
    assert all(item["usable_media_count"] == 0 for item in coverage["requirements"])
    assert len(good.calls) + len(bad.calls) <= 48
    assert len({json.dumps(call, sort_keys=True) for call in good.calls}) == len(good.calls)
    assert len({json.dumps(call, sort_keys=True) for call in bad.calls}) == len(bad.calls)

    items = list_project_media_items(channel, project.id)
    preserved = next(item for item in items if item.id == operator_item.id)
    assert preserved.notes == operator_item.notes and preserved.rights_status == "restricted"
    references = [
        item
        for item in items
        if item.external_reference_url and item.provenance.get("preparation_run_id") == str(run.id)
    ]
    assert len(references) == 1  # One canonical reference, linked to all supported requirements.
    reference = references[0]
    assert reference.external_reference_url == "https://publisher.example/press/vision"
    assert reference.status == "reference_only" and reference.rights_status == "not_assessed"
    assert reference.requirement_ids and set(reference.requirement_ids) <= requirement_ids
    receipts = completed["search_receipts"]
    assert {receipt["status"] for receipt in receipts} == {"succeeded", "failed"}
    assert len(receipts) == completed["progress"]["completed_jobs"]
    assert all(receipt["scope"]["channel_id"] == str(channel) for receipt in receipts)
    assert all(receipt["scope"]["project_id"] == str(project.id) for receipt in receipts)
    assert set(reference.provenance["scout_receipt_refs"]) <= {
        receipt["receipt_id"] for receipt in receipts
    }
    serialized = json.dumps(reference.provenance)
    assert "ssr_" in serialized
    assert reference.provenance["revision_digest"] == hashlib.sha256(text.encode()).hexdigest()
    assert "production_cleared" not in serialized and "sensitive upstream" not in json.dumps(
        completed, default=str
    )
    owned = [item for item in items if item.clip_id]
    assert len(owned) == 1 and owned[0].clip_id == clip_id
    assert owned[0].status == "acquired" and owned[0].rights_status == "not_assessed"
    with db.session_scope() as session:
        checkpoints = list(
            session.scalars(
                select(DomainEvent).where(
                    DomainEvent.event_type == "editorial.media_preparation_checkpoint"
                )
            )
        )
        assert len(checkpoints) > 3
        assert all(event.payload["channel_profile_id"] == str(channel) for event in checkpoints)
        assert all(event.payload["project_id"] == str(project.id) for event in checkpoints)
        assert checkpoints[-1].payload["status"] == "media_completed"
        clips = list(session.scalars(select(Clip)))
        assert len(clips) == 1 and clips[0].storage_key == "canonical/vision.mp4"
        assert clips[0].sha256 == "a" * 64

    calls_before = len(good.calls), len(bad.calls)
    replay = start(channel, project)
    assert replay.id == run.id
    assert finish(channel, project.id, run.id) == completed
    assert (len(good.calls), len(bad.calls)) == calls_before
    assert {item.id for item in list_project_media_items(channel, project.id)} == {
        item.id for item in items
    }


def test_pagination_and_resume_reopen_saved_ledger_without_duplicate_calls(script_to_bin):
    channel, _, good, bad, _ = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    run = start(channel, project)
    completed = finish(channel, project.id, run.id)
    assert completed["coverage"]["requirements"][0]["references"]
    assert Counter(bool(cursor) for _, cursor in good.calls) == {False: 1, True: 1}
    assert len(bad.calls) == 1
    before = json.dumps(completed, sort_keys=True, default=str)
    preparation.resume_preparation(channel, project.id, run.id)
    assert (
        json.dumps(
            preparation.response(preparation.get_preparation(channel, project.id, run.id)),
            sort_keys=True,
            default=str,
        )
        == before
    )
    assert len(good.calls) == 2 and len(bad.calls) == 1


def test_cross_channel_and_changed_revision_stop_before_provider_dispatch(script_to_bin):
    channel, other_channel, good, bad, _ = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    run = start(channel, project)
    for operation in (
        lambda: preparation.get_preparation(other_channel, project.id, run.id),
        lambda: preparation.resume_preparation(other_channel, project.id, run.id),
        lambda: preparation.latest_preparation(other_channel, project.id),
    ):
        with pytest.raises(ValueError):
            operation()
    save_draft(
        channel,
        project.id,
        SaveEditorialDraft.model_validate(
            {
                "expected_revision": get_project(channel, project.id).revision,
                "idempotency_key": "changed-script",
                "draft": {
                    "script": [
                        {
                            "id": "new-beat",
                            "role": "context",
                            "narration": "A changed story.",
                            "nonfactual": True,
                            "planned_duration_seconds": 12,
                            "visual_intent": "A completely different orchard",
                        }
                    ]
                },
            }
        ),
        actor="fixture-editor",
    )
    assert preparation.response(preparation.get_preparation(channel, project.id, run.id))["stale"]
    with pytest.raises(ValueError):
        preparation.resume_preparation(channel, project.id, run.id)
    assert not good.calls and not bad.calls
    assert not list_project_media_items(channel, project.id)


def test_saved_script_edits_override_imported_seed_for_new_preparation(script_to_bin):
    channel, _, good, bad, _ = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Old window direction]")
    saved = save_draft(
        channel,
        project.id,
        SaveEditorialDraft.model_validate(
            {
                "expected_revision": 0,
                "idempotency_key": "edited-script",
                "draft": {
                    "script": [
                        {
                            "id": "edited-beat",
                            "role": "context",
                            "narration": "New orchard narration.",
                            "nonfactual": True,
                            "planned_duration_seconds": 12,
                            "visual_intent": "Orchard trees at sunrise",
                        }
                    ]
                },
            }
        ),
        actor="fixture-editor",
    )
    run = start(channel, project, revision=saved.revision)
    manifest = preparation.response(run)["manifest"]
    assert "Orchard trees at sunrise" in manifest["plan"]["source_text"]
    assert "Old window direction" not in manifest["plan"]["source_text"]
    finish(channel, project.id, run.id)
    assert good.calls and bad.calls
    assert all("Old window" not in query["q"] for query, _ in good.calls)


@pytest.mark.parametrize("crash_seam", ["scout", "_publish"])
def test_crash_after_durable_evidence_or_bin_insert_replays_without_duplication(
    script_to_bin, monkeypatch, crash_seam
):
    channel, _, good, bad, _ = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    run = start(channel, project)
    original = getattr(preparation, crash_seam)
    raised = False

    def crash_once(*args, **kwargs):
        nonlocal raised
        result = original(*args, **kwargs)
        if not raised:
            raised = True
            raise RuntimeError("simulated process interruption after durable write")
        return result

    monkeypatch.setattr(preparation, crash_seam, crash_once)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        # Publication follows pagination in a later batch; receipt interruption
        # happens immediately. Both failures leave durable state independently.
        for _ in range(10):
            preparation.resume_preparation(channel, project.id, run.id)
    failed = preparation.response(preparation.get_preparation(channel, project.id, run.id))
    assert failed["status"] == "failed"
    assert "simulated process interruption" not in failed["error"]
    after_failure = list_project_media_items(channel, project.id)
    if crash_seam == "_publish":
        assert len(after_failure) == 1
    else:
        assert not after_failure
    completed = finish(channel, project.id, run.id)
    assert (
        completed["status"] == "completed"
        and completed["coverage"]["requirements"][0]["references"]
    )
    assert len(good.calls) == 2 and len(bad.calls) == 1
    items = list_project_media_items(channel, project.id)
    assert len(items) == 1 and items[0].rights_status == "not_assessed"
    if after_failure:
        assert items[0].id == after_failure[0].id


def test_api_authorization_and_channel_scope_cover_start_status_resume_and_bin(script_to_bin):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from katcha.api.editorial_media_preparation import router as preparation_router

    from katcha.api.editorial_projects import router as projects_router

    channel, other_channel, good, bad, _ = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    app = FastAPI()

    @app.middleware("http")
    async def fixture_control_auth(request, call_next):
        request.state.control_actor = "fixture-editor"
        request.state.control_principal_name = "fixture-editor"
        request.state.control_scopes = {"ai:read"}
        if request.headers.get("x-fixture-role") == "writer":
            request.state.control_scopes.add("production:create")
        request.state.control_channel_profile_ids = {
            request.headers.get("x-fixture-channel", str(channel))
        }
        return await call_next(request)

    app.include_router(projects_router)
    app.include_router(preparation_router)
    path = f"/v1/channels/{channel}/editorial-projects/{project.id}/media-preparation"
    body = {
        "idempotency_key": "http-preparation",
        "expected_revision": 0,
        "external_search_authorized": True,
    }
    with TestClient(app) as client:
        assert client.post(path, json=body).status_code == 403
        assert client.get(path).json() is None
        injected = client.post(
            path,
            json={**body, "max_jobs": 10_000, "providers": ["paid_adapter"]},
            headers={"x-fixture-role": "writer"},
        )
        assert injected.status_code == 422
        assert not good.calls and not bad.calls
        result = client.post(path, json=body, headers={"x-fixture-role": "writer"})
        assert result.status_code == 202
        run_id = result.json()["run_id"]
        assert client.get(f"{path}/{run_id}").json()["status"] == "pending"
        assert client.post(f"{path}/{run_id}/resume").status_code == 403
        assert (
            client.post(
                f"{path}/{run_id}/reconcile", json={"confirmed_worker_stopped": True}
            ).status_code
            == 403
        )
        wrong_path = path.replace(str(channel), str(other_channel))
        assert client.get(f"{wrong_path}/{run_id}").status_code == 403
        assert (
            client.get(
                f"{wrong_path}/{run_id}", headers={"x-fixture-channel": str(other_channel)}
            ).status_code
            == 404
        )
        assert not good.calls and not bad.calls
        for _ in range(10):
            result = client.post(f"{path}/{run_id}/resume", headers={"x-fixture-role": "writer"})
            assert result.status_code == 200
            if result.json()["status"] == "completed":
                break
        else:
            pytest.fail("HTTP fixture preparation did not complete")
        listed = client.get(path.replace("media-preparation", "media-bin"))
        assert listed.status_code == 200 and len(listed.json()) == 1
        assert listed.json()[0]["rights_status"] == "not_assessed"
        assert client.get(path).json()["run_id"] == run_id
        assert client.get(f"{path}/{run_id}").json()["coverage"]["usable_media_count"] == 0


def test_lost_authoritative_ledger_never_repeats_previously_observed_provider_calls(script_to_bin):
    channel, _, good, bad, ledger_path = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    run = start(channel, project)
    preparation.resume_preparation(channel, project.id, run.id)
    before = len(good.calls), len(bad.calls)
    assert before == (1, 1)
    ledger_path.unlink()
    with pytest.raises(ValueError, match="ledger|evidence"):
        preparation.resume_preparation(channel, project.id, run.id)
    assert (len(good.calls), len(bad.calls)) == before
    assert not list_project_media_items(channel, project.id)


def test_revision_change_during_scouting_fences_later_calls_and_bin_publication(
    script_to_bin, monkeypatch
):
    channel, _, good, bad, _ = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    run = start(channel, project)
    original = preparation.scout
    changed = False

    def change_after_receipt(*args, **kwargs):
        nonlocal changed
        page = original(*args, **kwargs)
        if not changed:
            changed = True
            save_draft(
                channel,
                project.id,
                SaveEditorialDraft.model_validate(
                    {
                        "expected_revision": get_project(channel, project.id).revision,
                        "idempotency_key": "concurrent-editor-save",
                        "draft": {
                            "script": [
                                {
                                    "id": "new-beat",
                                    "role": "context",
                                    "narration": "Updated narration.",
                                    "nonfactual": True,
                                    "planned_duration_seconds": 12,
                                    "visual_intent": "New orchard scene",
                                }
                            ]
                        },
                    }
                ),
                actor="fixture-editor",
            )
        return page

    monkeypatch.setattr(preparation, "scout", change_after_receipt)
    with pytest.raises(ValueError, match="changed"):
        preparation.resume_preparation(channel, project.id, run.id)
    assert len(good.calls) == 1 and not bad.calls
    assert not list_project_media_items(channel, project.id)
    assert preparation.response(preparation.get_preparation(channel, project.id, run.id))["stale"]


def test_active_preparation_lease_and_running_receipt_do_not_duplicate_or_complete_work(
    script_to_bin,
    monkeypatch,
):
    from datetime import UTC, datetime, timedelta

    from katcha.editorial.media_preparation_schemas import ReconcileMediaPreparation

    from katcha.editorial.source_intelligence.contracts import SourceSearchJobV1
    from katcha.editorial_models import EditorialRun

    channel, _, good, bad, ledger_path = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    run = start(channel, project)
    with db.session_scope() as session:
        persisted = session.get(EditorialRun, run.id)
        persisted.status = "media_running"
        persisted.artifacts = {
            **persisted.artifacts,
            "lease_token": "another-worker",
            "lease_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
    active = preparation.resume_preparation(channel, project.id, run.id)
    assert preparation.response(active)["status"] == "running"
    assert not good.calls and not bad.calls
    with db.session_scope() as session:
        persisted = session.get(EditorialRun, run.id)
        persisted.status = "media_pending"
        persisted.artifacts = {**persisted.artifacts, "lease_until": None, "lease_token": None}
    job = SourceSearchJobV1.model_validate(run.artifacts["jobs"][0])
    configured = preparation.runtime()
    route = next(
        item for item in configured.routes if item.capability.provider_key == job.provider_key
    )
    receipt, claimed = configured.ledger._claim(job, route.policy, route.capability, retry=False)
    assert claimed and receipt.status == "running"
    paused = preparation.response(preparation.resume_preparation(channel, project.id, run.id))
    assert paused["status"] == "failed" and paused["stage"] == "media_outcome_unknown"
    assert paused["coverage"] is not None
    assert paused["progress"]["completed_jobs"] < paused["progress"]["total_jobs"]
    assert not good.calls and len(bad.calls) == 1
    assert not list_project_media_items(channel, project.id)
    assert SearchJobLedger(ledger_path).read(job.job_id, scope=job.scope).status == "running"
    with pytest.raises(ValueError):
        preparation.reconcile_preparation(
            channel, project.id, run.id, ReconcileMediaPreparation(confirmed_worker_stopped=False)
        )
    with pytest.raises(ValueError):
        preparation.reconcile_preparation(
            channel, project.id, run.id, ReconcileMediaPreparation(confirmed_worker_stopped=True)
        )

    # Advance the service clock only: the operator confirmation cannot bypass
    # the worker lease, and no provider outcome is reconciled automatically.
    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=11)

    monkeypatch.setattr(preparation, "datetime", LaterClock)
    recovered = preparation.reconcile_preparation(
        channel, project.id, run.id, ReconcileMediaPreparation(confirmed_worker_stopped=True)
    )
    assert preparation.response(recovered)["status"] == "pending"
    assert SearchJobLedger(ledger_path).read(job.job_id, scope=job.scope).status == "unknown"
    completed = finish(channel, project.id, run.id)
    assert completed["status"] == "completed"
    assert not good.calls and len(bad.calls) == 1
    assert not list_project_media_items(channel, project.id)
    assert any(receipt["status"] == "unknown" for receipt in completed["search_receipts"])


@pytest.mark.parametrize("policy_case", ["external_not_authorized", "paid_adapter"])
def test_operator_authorization_and_paid_provider_guards_preserve_honest_gaps(
    script_to_bin, monkeypatch, policy_case
):
    from dataclasses import replace

    channel, _, good, bad, _ = script_to_bin
    project, clip_id = imported_project(channel, "[VISUAL: Vision at a window]", owned_clip=True)
    configured = preparation.runtime()
    routes = tuple(
        replace(
            route,
            capability=replace(
                route.capability, external=True, potentially_paid=policy_case == "paid_adapter"
            ),
        )
        for route in configured.routes
    )
    monkeypatch.setattr(preparation, "runtime", lambda: replace(configured, routes=routes))
    run = start(channel, project, authorized=policy_case == "paid_adapter")
    pending = preparation.response(run)
    assert pending["progress"]["total_jobs"] == 0
    expected = {
        "external_not_authorized": "external_search_not_authorized",
        "paid_adapter": "paid_adapter_has_no_enforced_request_ceiling",
    }[policy_case]
    assert any(gap["category"] == expected for gap in pending["planning_gaps"])
    completed = finish(channel, project.id, run.id)
    assert completed["status"] == "completed"
    assert not good.calls and not bad.calls
    items = list_project_media_items(channel, project.id)
    assert len(items) == 1 and items[0].clip_id == clip_id
    assert items[0].status == "acquired" and items[0].rights_status == "not_assessed"
    assert completed["coverage"]["usable_media_count"] == 0


def test_legacy_editorial_history_remains_visible_and_media_preparation_is_separate(script_to_bin):
    from starlette.requests import Request

    from katcha.api.editorial_runs import list_runs
    from katcha.editorial_models import EditorialRun

    channel, _, _, _, _ = script_to_bin
    project, _ = imported_project(channel, "[VISUAL: Vision at a window]")
    preparation_run = start(channel, project)
    legacy_id = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            EditorialRun(
                id=legacy_id,
                project_id=project.id,
                channel_profile_id=channel,
                input_revision=preparation_run.input_revision,
                input_digest="b" * 64,
                options={},
                status="completed",
                stage="analysis",
                artifacts={},
                actor="fixture-editor",
            )
        )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/fixture",
            "headers": [],
            "query_string": b"",
            "state": {
                "control_actor": "fixture-editor",
                "control_principal_name": "fixture-editor",
                "control_scopes": {"ai:read"},
                "control_channel_profile_ids": {str(channel)},
            },
        }
    )
    rows = list_runs(channel, project.id, request, offset=0, limit=20, before=None)
    assert len(rows) == 1 and rows[0]["editorial_run_id"] == str(legacy_id)
    assert rows[0]["target"] == "analysis"
    assert preparation.latest_preparation(channel, project.id).id == preparation_run.id
