import uuid
from decimal import Decimal

from sqlalchemy import select

from katcha.db import session_scope
from katcha.models import DomainEvent, UsageEvent
from katcha.services.command_observability import (
    command_observability_summary,
    record_command_observation,
)


def test_command_observability_summarizes_latency_cost_and_actions() -> None:
    channel_id = uuid.uuid4()
    request_id = uuid.uuid4()
    thread_id = uuid.uuid4()

    record_command_observation(
        channel_profile_id=channel_id,
        request_id=request_id,
        thread_id=thread_id,
        actor="control-principal:operator",
        credential_id="operator-2026-q4",
        credential_fingerprint="def456abc123",
        intent="resource_context",
        planning_source="deterministic",
        planning_provider="katcha",
        planning_model="deterministic-command-router-v1",
        planning_confidence=1.0,
        narrator_provider="openai",
        narrator_model="fixture-model",
        narrator_degraded_reason=None,
        latency_ms=240,
        evidence_count=2,
        action_count=1,
        resource_kinds=["publication"],
    )

    with session_scope() as session:
        command_event = session.scalar(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == str(request_id),
                DomainEvent.event_type == "command_center.command_completed",
            )
        )
        assert command_event is not None
        assert command_event.payload["credential_id"] == "operator-2026-q4"
        assert (
            command_event.payload["credential_fingerprint"]
            == "def456abc123"
        )

        session.add(
            UsageEvent(
                task="performance_analysis",
                provider="openai",
                model="fixture-model",
                input_units=120,
                output_units=30,
                cost_usd=Decimal("0.0042"),
                reference_type="command_center",
                reference_id=str(request_id),
                usage_metadata={},
            )
        )
        for event_type in (
            "command_center.proposal_created",
            "command_center.action_executed",
        ):
            session.add(
                DomainEvent(
                    aggregate_type="command_action_proposal",
                    aggregate_id=str(uuid.uuid4()),
                    event_type=event_type,
                    payload={
                        "channel_profile_id": str(channel_id),
                        "request_id": str(request_id),
                    },
                )
            )

    result = command_observability_summary(channel_id, hours=24)

    assert result["request_count"] == 1
    assert result["average_latency_ms"] == 240
    assert result["p95_latency_ms"] == 240
    assert result["typed_context_request_count"] == 1
    assert result["estimated_cost_usd"] == 0.0042
    assert result["input_units"] == 120
    assert result["output_units"] == 30
    assert result["actions"] == {
        "proposed": 1,
        "executed": 1,
        "failed": 0,
    }
    assert result["recent"][0]["request_id"] == str(request_id)


def test_command_observability_is_channel_scoped() -> None:
    channel_id = uuid.uuid4()
    other_channel_id = uuid.uuid4()

    record_command_observation(
        channel_profile_id=other_channel_id,
        request_id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        actor="control-principal:other",
        intent="channel_status",
        planning_source="deterministic",
        planning_provider="katcha",
        planning_model="deterministic-command-router-v1",
        planning_confidence=1.0,
        narrator_provider="katcha",
        narrator_model="grounded-deterministic-v1",
        narrator_degraded_reason="fixture",
        latency_ms=999,
        evidence_count=0,
        action_count=0,
        resource_kinds=[],
    )

    result = command_observability_summary(channel_id, hours=24)

    assert result["request_count"] == 0
    assert result["estimated_cost_usd"] == 0.0
