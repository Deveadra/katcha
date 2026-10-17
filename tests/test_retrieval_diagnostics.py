"""Offline regressions for the failed scene-retrieval conversation; no provider calls."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_command_goals import receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.models import SourceItem
from katcha.orchestration import discovery_workflows, workflows
from katcha.orchestration.failure_details import failure_details
from katcha.services import goal_tools
from katcha.services.retrieval_diagnostics import source_item_status

saved = saved_fixture


def nested_failure():
    provider = RuntimeError(
        "HTTP 403: authentication required https://example.invalid/?token=hidden"
    )
    activity = RuntimeError("Activity task failed")
    activity.__cause__ = provider
    child = RuntimeError("Child Workflow execution failed")
    child.__cause__ = activity
    return child


def test_diagnostic_retains_underlying_failure_without_signed_url():
    detail = failure_details(nested_failure())
    assert "HTTP 403: authentication required" in detail["message"]
    assert "hidden" not in str(detail)
    assert len(detail["causes"]) == 3
    cycle = RuntimeError("Workflow execution failed")
    cycle.__cause__ = cycle
    assert len(failure_details(cycle)["causes"]) == 1


async def test_ingest_saves_cause_instead_of_temporal_wrapper(monkeypatch):
    calls = []

    async def execute(name, *args, **kwargs):
        calls.append((name, kwargs))
        if name == "ingest_source":
            raise nested_failure()

    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    with pytest.raises(RuntimeError, match="Child Workflow"):
        await workflows.ClipIngestWorkflow().run("source")
    assert calls[-1][0] == "mark_source_failed"
    assert "HTTP 403" in calls[-1][1]["args"][1]
    assert "hidden" not in calls[-1][1]["args"][1]


@pytest.mark.parametrize("mode", ["failure", "missing_clip", "acquired"])
async def test_preparation_requires_clip_and_preserves_diagnostics(monkeypatch, mode):
    prepared = {
        "prepared": [
            {"source_id": "media-source", "candidate_id": "candidate", "workflow_id": "ingest-1"}
        ],
        "skipped": False,
    }
    activities = AsyncMock(side_effect=[prepared, {}])
    children = AsyncMock(
        side_effect=[{"candidate_count": 1}, nested_failure()] if mode == "failure" else None
    )
    if mode != "failure":
        children.side_effect = [
            {"candidate_count": 1},
            {"clip_id": "clip"} if mode == "acquired" else {},
        ]
    monkeypatch.setattr(discovery_workflows.workflow, "execute_activity", activities)
    monkeypatch.setattr(discovery_workflows.workflow, "execute_child_workflow", children)
    monkeypatch.setattr(
        discovery_workflows.workflow, "info", lambda: SimpleNamespace(workflow_id="prepare")
    )
    result = await discovery_workflows.CommandSourcePrepareWorkflow().run("run", "ingest")
    entry = result["ingests"][0]
    assert entry["source_item_id"] == "media-source"
    assert entry["candidate_id"] == "candidate"
    assert entry["success"] == (mode == "acquired")
    assert result["status"] == ("completed" if mode == "acquired" else "failed")
    assert result["scene_verified"] is False
    if mode == "failure":
        assert "HTTP 403" in entry["error"]
        assert "hidden" not in str(entry)


def test_media_source_is_distinct_and_channel_scoped(saved):
    channel = saved[0]
    identity = uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            SourceItem(
                id=identity,
                source_url="https://example.invalid/clip",
                canonical_url="https://example.invalid/clip",
                platform="youtube",
                status="failed",
                error="HTTP 403",
                workflow_id="ingest-1",
                source_metadata={"channel_profile_id": str(channel)},
            )
        )
    detail = source_item_status(channel, identity)
    assert detail["source_item_id"] == str(identity)
    assert detail["error"] == "HTTP 403"
    assert detail["clip_id"] is None
    with pytest.raises(ValueError, match="not found in this channel"):
        source_item_status(uuid.uuid4(), identity)


async def test_workflow_status_returns_failure_cause(saved, monkeypatch):
    goal = receipt(saved[0])
    monkeypatch.setattr(goal_tools, "observed_workflow_id", lambda *args: "ingest-1")
    handle = SimpleNamespace(
        describe=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name="FAILED"))),
        result=AsyncMock(side_effect=nested_failure()),
    )
    monkeypatch.setattr(
        "katcha.orchestration.client.get_temporal_client",
        AsyncMock(return_value=SimpleNamespace(get_workflow_handle=lambda _: handle)),
    )
    result = await goal_tools.run_read_tool(goal, "workflow_status", {"workflow_id": "ingest-1"})
    assert result["workflow_state"] == "FAILED"
    assert "HTTP 403" in result["diagnostic"]["message"]


async def test_page_evidence_is_not_video_verification(saved, monkeypatch):
    goal = receipt(saved[0])
    monkeypatch.setattr(
        "katcha.editorial.retrieval.retrieve_document",
        lambda url: {
            "url": url,
            "text": "page evidence",
            "sha256": "fixture",
            "retrieved_at": "fixture",
        },
    )
    result = await goal_tools.run_read_tool(goal, "read_web_page", {"url": "https://example.com"})
    assert result["evidence_kind"] == "page_text"
    assert result["scene_verified"] is False


async def test_page_transport_failure_is_a_diagnostic_observation(saved, monkeypatch):
    def unavailable(url):
        raise ConnectionError("connection failed")

    monkeypatch.setattr("katcha.editorial.retrieval.retrieve_document", unavailable)
    result = await goal_tools.run_read_tool(
        receipt(saved[0]), "read_web_page", {"url": "https://example.com"}
    )
    assert result["available"] is False
    assert result["diagnostic"]["message"] == "connection failed"


@pytest.mark.parametrize(
    "message",
    [
        "access_token=supersecret",
        "api-key=supersecret",
        "refresh_token=supersecret",
        '{"token": "supersecret"}',
        "{'password': 'supersecret'}",
        "Authorization: Bearer supersecret",
    ],
)
def test_secret_variants_are_redacted(message):
    assert "supersecret" not in str(failure_details(RuntimeError(message)))


async def test_temporal_conversion_preserves_failure_stage():
    from temporalio.converter import DataConverter
    from temporalio.exceptions import ApplicationError

    provider = RuntimeError("HTTP 403")
    outer = ApplicationError(
        "media inspection failed",
        {"stage": "media inspection (ffprobe)"},
        type="MediaIngestionFailure",
    )
    outer.__cause__ = provider
    from temporalio.api.failure.v1 import Failure

    encoded = Failure()
    converter = DataConverter.default
    await converter.encode_failure(outer, encoded)
    restored = await converter.decode_failure(encoded)
    detail = failure_details(restored)
    assert detail["stage"] == "media inspection (ffprobe)"
    assert "HTTP 403" in detail["message"]


async def test_failed_attempt_diagnostic_is_durable(saved, monkeypatch):
    from katcha.integrations.download import MediaDownloadError
    from katcha.orchestration import activities

    identity, channel = uuid.uuid4(), saved[0]
    with db.session_scope() as session:
        session.add(
            SourceItem(
                id=identity,
                source_url="https://example.invalid/failure",
                canonical_url="https://example.invalid/failure",
                platform="youtube",
                status="registered",
                source_metadata={"channel_profile_id": str(channel)},
            )
        )

    def fail(url):
        raise MediaDownloadError("download", RuntimeError("HTTP 403"))

    monkeypatch.setattr(activities, "download", fail)
    from temporalio.exceptions import ApplicationError

    with pytest.raises(ApplicationError, match="HTTP 403"):
        activities.ingest_source(str(identity))
    detail = source_item_status(channel, identity)
    assert detail["diagnostic"]["stage"] == "download"
    assert detail["status"] == "ingesting"  # final failure is owned by the workflow


async def test_malformed_preparation_cannot_complete(monkeypatch):
    activity = AsyncMock(side_effect=[{"prepared": [{"candidate_id": "candidate"}]}, {}])
    monkeypatch.setattr(discovery_workflows.workflow, "execute_activity", activity)
    monkeypatch.setattr(
        discovery_workflows.workflow,
        "execute_child_workflow",
        AsyncMock(return_value={"candidate_count": 1}),
    )
    monkeypatch.setattr(
        discovery_workflows.workflow, "info", lambda: SimpleNamespace(workflow_id="prepare")
    )
    result = await discovery_workflows.CommandSourcePrepareWorkflow().run("run", "ingest")
    assert result["status"] == "failed"
    assert result["ingests"][0]["success"] is False


def test_retrieval_completion_requires_every_independent_target(saved):
    from katcha.ai.goal_planner import GoalDecision, RetrievedTarget
    from katcha.services.goal_runner import _retrieval_completion_error

    goal = receipt(saved[0])
    goal.authorization = {
        "retrieval_targets": [
            {"description": "Kate Bishop coin", "scene_specific": True},
            {"description": "Ultron coin", "scene_specific": True},
        ]
    }
    decision = GoalDecision(outcome="complete", reason="done", confidence=1)
    assert "Every requested clip" in _retrieval_completion_error(goal, decision)
    clip = uuid.uuid4()
    decision.retrieved_targets = [RetrievedTarget(target_index=i, clip_id=clip) for i in range(2)]
    assert "same media" in _retrieval_completion_error(goal, decision)


def test_acquired_candidate_cannot_complete_exact_scene_request(saved):
    from katcha.ai.goal_planner import GoalDecision, RetrievedTarget
    from katcha.models import Clip
    from katcha.services.goal_runner import _retrieval_completion_error

    channel, clip_id = saved[0], uuid.uuid4()
    with db.session_scope() as session:
        session.add(
            Clip(id=clip_id, sha256="a" * 64, storage_key="fixture/clip", status="ingested")
        )
        session.add(
            SourceItem(
                source_url="https://example.invalid/acquired",
                canonical_url="https://example.invalid/acquired",
                platform="youtube",
                clip_id=clip_id,
                status="ready",
                source_metadata={"channel_profile_id": str(channel)},
            )
        )
    goal = receipt(channel)
    goal.request = {**goal.request, "selected_clip_ids": [str(clip_id)]}
    goal.authorization = {
        "retrieval_targets": [{"description": "coin throw", "scene_specific": True}]
    }
    decision = GoalDecision(
        outcome="complete",
        reason="done",
        confidence=1,
        retrieved_targets=[RetrievedTarget(target_index=0, clip_id=clip_id)],
    )
    assert "unverified" in _retrieval_completion_error(goal, decision)
    # Downloading a whole source does not require proving a scene match.
    goal.authorization["retrieval_targets"][0]["scene_specific"] = False
    assert _retrieval_completion_error(goal, decision) is None


async def test_youtube_challenge_does_not_schedule_futile_activity_retries(saved, monkeypatch):
    from temporalio.api.failure.v1 import Failure
    from temporalio.converter import DataConverter
    from temporalio.exceptions import ApplicationError

    from katcha.integrations.download import DownloadAuthenticationRequired
    from katcha.orchestration import activities

    def challenge(source_id):
        raise DownloadAuthenticationRequired("download", RuntimeError("Sign in to confirm"))

    monkeypatch.setattr(activities, "_ingest_source", challenge)
    with pytest.raises(ApplicationError) as caught:
        activities.ingest_source(str(uuid.uuid4()))
    assert caught.value.non_retryable is True
    encoded = Failure()
    await DataConverter.default.encode_failure(caught.value, encoded)
    restored = await DataConverter.default.decode_failure(encoded)
    detail = failure_details(restored)
    assert detail["code"] == "youtube_authentication_required"
    assert detail["retryable"] is False
    assert "server" in detail["recovery"]


def test_historical_youtube_failure_gets_same_server_recovery():
    legacy = RuntimeError("ERROR: [youtube] fixture: Sign in to confirm you’re not a bot")
    detail = failure_details(legacy)
    assert detail["code"] == "youtube_authentication_required"
    assert detail["retryable"] is False
    assert "server" in detail["recovery"]
