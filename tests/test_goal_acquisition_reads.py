"""Real native API reads of synthetic acquisition state; no provider or retry calls."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from test_command_goals import receipt
from test_command_goals import saved as saved_fixture

from katcha import db
from katcha.acquisition_job_models import AcquisitionJob
from katcha.acquisition_models import DiscoveryCandidate
from katcha.editorial_models import AcquisitionReceipt
from katcha.intelligence_models import ChannelProfile
from katcha.services.goal_tools import TOOLS, native_route_tool, run_read_tool, tool_schema


@pytest.fixture
def saved(monkeypatch):
    yield from saved_fixture.__wrapped__(monkeypatch)


def job(channel, *, status="waiting_human"):
    candidate, receipt_id, identity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with db.session_scope() as session:
        session.add(DiscoveryCandidate(
            id=candidate, adapter_key="licensed", platform="web",
            source_url=f"https://media.example/{candidate}",
            canonical_url=f"https://media.example/{candidate}",
            candidate_metadata={"channel_profile_id": str(channel)},
        ))
        session.flush()
        session.add(AcquisitionReceipt(
            id=receipt_id, channel_profile_id=channel, candidate_id=candidate,
            operation_key=str(identity), request_digest="a" * 64,
            status="acquiring", authorization_scope={}, request_time=datetime.now(UTC),
            fetch_url_if_allowed="https://media.example/private?token=fixture-secret",
        ))
        session.flush()
        session.add(AcquisitionJob(
            id=identity, receipt_id=receipt_id, channel_profile_id=channel,
            status=status, max_bytes=10000, generation=1, phase="awaiting_operator",
            bytes_received=0, failure_code="authentication_required",
            pending_result={"private": "fixture-secret"},
        ))
    return str(identity)


def test_catalog_only_exposes_reads_and_native_filters():
    for name in ["acquisition_jobs", "acquisition_job"]:
        assert TOOLS[name].scope == "channels:read"
        assert TOOLS[name].method == "GET" and not TOOLS[name].mutates
    schema = tool_schema("acquisition_jobs")
    assert "status" in schema["properties"]["query"]["properties"]
    assert "job_id" in tool_schema("acquisition_job")["properties"]["path"]["properties"]
    assert native_route_tool("/v1/channels/channel/acquisition-jobs/job/human", "GET") is None
    assert not any("/human" in tool.route for tool in TOOLS.values())


async def test_empty_channel_is_not_a_failed_download(saved):
    goal = receipt(saved[0], "Explain whether acquisition is working")
    actual = await run_read_tool(goal, "acquisition_jobs", {})
    assert actual["items"] == [] and actual["total"] == 0


async def test_channel_jobs_report_waiting_without_private_payloads_or_false_success(saved):
    channel = saved[0]
    own = job(channel)
    other = uuid.uuid4()
    with db.session_scope() as session:
        session.add(ChannelProfile(id=other, youtube_connection_id=uuid.uuid4()))
    foreign = job(other)
    goal = receipt(channel, "Explain the blocked download")
    actual = await run_read_tool(goal, "acquisition_jobs", {"query": {"status": "waiting_human"}})
    assert [row["id"] for row in actual["items"]] == [own]
    assert foreign not in str(actual) and "fixture-secret" not in str(actual)
    assert actual["items"][0]["media_verified"] is False
    goal.observations = [{"tool": "acquisition_jobs", "result": actual}]
    detail = await run_read_tool(goal, "acquisition_job", {"path": {"job_id": own}})
    assert detail["status"] == "waiting_human"
    assert detail["failure_code"] == "authentication_required"
    assert detail["bytes_received"] == 0
    with pytest.raises(ValueError, match="not observed"):
        await run_read_tool(goal, "acquisition_job", {"path": {"job_id": str(uuid.uuid4())}})
    # Even a previously observed ID cannot cross the current channel boundary.
    goal.observations += [{"id": foreign}]
    with pytest.raises(HTTPException) as blocked:
        await run_read_tool(goal, "acquisition_job", {"path": {"job_id": foreign}})
    assert blocked.value.status_code == 403


async def test_job_reads_reject_channel_override_and_unsupported_filters(saved):
    goal = receipt(saved[0])
    with pytest.raises(ValueError, match="different channel"):
        await run_read_tool(goal, "acquisition_jobs", {
            "path": {"channel_profile_id": str(uuid.uuid4())},
        })
    with pytest.raises(ValueError, match="unsupported query filters"):
        await run_read_tool(goal, "acquisition_jobs", {"query": {"fetch_url": "private"}})
