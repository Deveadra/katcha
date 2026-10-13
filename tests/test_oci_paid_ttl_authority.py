"""Safety regression: TTL expiry is never permission to stop production."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from katcha.ops import oci_recovery

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def paid(*, state="STOPPED", authorized=True, epoch="8", expires=None):
    return {
        "id": "paid-old",
        "lifecycle-state": state,
        "freeform-tags": {
            "KatchaPaidFallback": "true",
            "KatchaRecoveryMode": "paid-fallback",
            "KatchaDeploymentId": "paid-deployment",
            "KatchaDeploymentEpoch": epoch,
            "KatchaPaidCleanupAuthorized": "true" if authorized else "false",
            "KatchaExpiresAt": (
                expires or (NOW - timedelta(minutes=2))
            ).isoformat(),
            "KatchaBudgetReservationId": "reservation",
            "KatchaBudgetReservedMicrousd": "1200000",
            "KatchaPaidStartedAt": (NOW - timedelta(hours=2)).isoformat(),
            "KatchaEstimatedHourlyUsd": "0.10",
        },
    }


def successor():
    return {
        "id": "new-leader",
        "lifecycle-state": "RUNNING",
        "freeform-tags": {
            "KatchaDeploymentId": "new-deployment",
            "KatchaDeploymentEpoch": "9",
        },
    }


class FakeOci:
    def __init__(self, row=None):
        self.row = deepcopy(row or paid())
        self.leader = successor()
        self.terminated = []
        self.stopped = []
        self.lookups = 0
        self.on_get = None

    def list_instances(self, _config):
        return [deepcopy(self.row), deepcopy(self.leader)]

    def get_instance(self, instance_id):
        assert instance_id == "paid-old"
        self.lookups += 1
        if self.on_get:
            self.on_get(self)
        return deepcopy(self.row)

    def softstop(self, instance_id):
        self.stopped.append(instance_id)
        pytest.fail("TTL cleanup must never issue SOFTSTOP")

    def terminate(self, instance_id):
        self.terminated.append(instance_id)


class Coordinator:
    def __init__(self):
        self.authority = {
            "active": {"deployment_id": "new-deployment", "epoch": 9},
            "pending": None,
        }
        self.called = 0
        self.settled = []
        self.on_status = None

    def status(self):
        self.called += 1
        if self.on_status:
            self.on_status(self)
        return deepcopy(self.authority)

    def settle_external_compute(self, reservation_id, *, actual_cost_microusd, metadata):
        self.settled.append((reservation_id, actual_cost_microusd))
        return {"reservation": {"status": "settled"}}


def execute(oci, coordinator):
    return oci_recovery.cleanup_expired_paid(object(), oci, coordinator, now=NOW)


def assert_preserved(oci, coordinator, match):
    with pytest.raises(oci_recovery.RecoveryError, match=match):
        execute(oci, coordinator)
    assert oci.stopped == []
    assert oci.terminated == []
    assert coordinator.settled == []


@pytest.mark.parametrize("state", ["RUNNING", "STARTING", "STOPPING"])
def test_expired_paid_primary_or_transitional_instance_is_never_stopped(state):
    oci = FakeOci(paid(state=state))
    coordinator = Coordinator()
    coordinator.authority["active"] = {
        "deployment_id": "paid-deployment", "epoch": 8,
    }
    assert_preserved(oci, coordinator, "not STOPPED")


@pytest.mark.parametrize(
    "active",
    [
        {"deployment_id": "paid-deployment", "epoch": 8},
        {"deployment_id": "new-deployment", "epoch": 8},
        {"deployment_id": "new-deployment", "epoch": None},
        {"deployment_id": "new-deployment", "epoch": True},
        {"deployment_id": "", "epoch": 9},
        None,
    ],
)
def test_expired_stopped_active_missing_or_stale_authority_never_deleted(active):
    oci = FakeOci()
    coordinator = Coordinator()
    coordinator.authority["active"] = active
    assert_preserved(oci, coordinator, "authority|handover")


@pytest.mark.parametrize("pending", [
    {"deployment_id": "paid-deployment", "epoch": 10},
    {"deployment_id": "different", "epoch": 10},
])
def test_pending_candidate_blocks_cleanup_even_of_stopped_vm(pending):
    oci = FakeOci()
    coordinator = Coordinator()
    coordinator.authority["pending"] = pending
    assert_preserved(oci, coordinator, "pending handover")


@pytest.mark.parametrize("tag", [
    "KatchaDeploymentId", "KatchaDeploymentEpoch", "KatchaPaidCleanupAuthorized",
])
def test_missing_deployment_or_retirement_tags_block_termination(tag):
    row = paid()
    del row["freeform-tags"][tag]
    assert_preserved(FakeOci(row), Coordinator(), "complete deployment tags")


@pytest.mark.parametrize("state", ["RUNNING", "STOPPED", "TERMINATING"])
def test_missing_expiration_does_not_issue_softstop(state):
    row = paid(state=state)
    del row["freeform-tags"]["KatchaExpiresAt"]
    # TERMINATING resources are untouched already, while other states fail.
    oci = FakeOci(row)
    coordinator = Coordinator()
    if state == "TERMINATING":
        assert execute(oci, coordinator) == []
        assert oci.terminated == []
    else:
        assert_preserved(oci, coordinator, "expiration missing or invalid")


def test_failed_coordinator_api_blocks_all_mutations():
    class Failed(Coordinator):
        def status(self):
            raise oci_recovery.RecoveryError("coordinator unavailable")

    assert_preserved(FakeOci(), Failed(), "coordinator unavailable")


@pytest.mark.parametrize("change", ["instance_missing", "not_running", "wrong_epoch"])
def test_successor_must_be_unique_running_and_match_committed_epoch(change):
    oci = FakeOci()
    if change == "instance_missing":
        oci.leader["freeform-tags"]["KatchaDeploymentId"] = "other"
    elif change == "not_running":
        oci.leader["lifecycle-state"] = "STOPPED"
    else:
        oci.leader["freeform-tags"]["KatchaDeploymentEpoch"] = "10"
    assert_preserved(oci, Coordinator(), "successor")


def test_revoked_cleanup_authorization_blocks_even_stopped_expired():
    assert_preserved(FakeOci(paid(authorized=False)), Coordinator(), "authorized")


def test_missing_price_or_expiry_never_stops_unknown_running_instance():
    row = paid(state="RUNNING")
    row["freeform-tags"]["KatchaExpiresAt"] = "not-a-date"
    assert_preserved(FakeOci(row), Coordinator(), "expiration missing or invalid")


def test_target_changes_after_first_authority_snapshot():
    oci = FakeOci()

    def drift(fake):
        fake.row["lifecycle-state"] = "RUNNING"

    oci.on_get = drift
    assert_preserved(oci, Coordinator(), "target identity or STOPPED state changed")


def test_coordinator_changes_during_verification_prevents_termination():
    oci = FakeOci()
    coordinator = Coordinator()

    def flip_state(mock):
        if mock.called == 2:
            mock.authority["active"]["epoch"] = 10

    coordinator.on_status = flip_state
    assert_preserved(oci, coordinator, "authority changed")


def test_valid_stopped_expired_retired_paid_can_be_deleted_only_after_two_checks():
    oci = FakeOci()
    coordinator = Coordinator()
    assert execute(oci, coordinator) == ["paid-old"]
    assert coordinator.called == 2
    assert oci.lookups == 1
    assert oci.terminated == ["paid-old"]
    assert oci.stopped == []
    assert coordinator.settled == [("reservation", 198_334)]


def test_future_stopped_paid_instance_never_deleted():
    oci = FakeOci(paid(expires=NOW + timedelta(hours=1)))
    coordinator = Coordinator()
    assert execute(oci, coordinator) == []
    assert oci.terminated == []
    assert coordinator.settled == []


def test_terminated_instance_is_not_retried_or_mutated():
    oci = FakeOci(paid(state="TERMINATED"))
    coordinator = Coordinator()
    assert execute(oci, coordinator) == []
    assert oci.terminated == []
    assert coordinator.settled == []


def test_termination_error_never_settles_budget_as_finished():
    class Broken(FakeOci):
        def terminate(self, instance_id):
            raise oci_recovery.RecoveryError("termination status unknown")

    oci = Broken()
    coordinator = Coordinator()
    assert_preserved(oci, coordinator, "termination status unknown")
