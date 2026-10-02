import test from "node:test";
import assert from "node:assert/strict";

import {
  StateConflict,
  abortPending,
  applyProbe,
  commitAuthority,
  configureWatchdog,
  defaultAuthorityState,
  evaluatePaidFallback,
  fenceResult,
  markIncidentDispatched,
  prepareAuthority,
  recordRecoveryOutcome,
} from "../src/state.mjs";

test("prepare allocates a monotonic epoch without fencing the active leader", () => {
  const first = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-a1",
    healthUrl: "https://a1.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  }, 1000);
  assert.equal(first.pending.epoch, 1);
  assert.equal(first.state.active, null);

  const committed = commitAuthority(first.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  }, 2000);
  const second = prepareAuthority(committed.state, {
    deploymentId: "oci-paid-fallback",
    healthUrl: "https://paid.example.test/v1/health/ready",
    expectedActiveEpoch: 1,
  }, 3000);

  assert.equal(fenceResult(second.state, "oci-a1", 1).authorized, true);
  assert.equal(fenceResult(second.state, "oci-paid-fallback", 2).authorized, false);
  assert.equal(second.pending.epoch, 2);
});

test("commit atomically transfers authority to the prepared deployment", () => {
  const prepared = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-a1",
    healthUrl: "https://a1.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  });
  const committed = commitAuthority(prepared.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  });

  assert.equal(fenceResult(committed.state, "oci-a1", 1).authorized, true);
  assert.equal(fenceResult(committed.state, "old-host", 0).authorized, false);
  assert.equal(committed.state.pending, null);
});

test("stale expected epochs cannot prepare or commit authority", () => {
  const prepared = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-a1",
    healthUrl: "https://a1.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  });
  const committed = commitAuthority(prepared.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  });

  assert.throws(
    () => prepareAuthority(committed.state, {
      deploymentId: "other",
      healthUrl: "https://other.example.test/v1/health/ready",
      expectedActiveEpoch: 0,
    }),
    StateConflict,
  );
});

test("prepare is idempotent and abort never changes active authority", () => {
  const initial = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "candidate",
    healthUrl: "https://candidate.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  });
  const replay = prepareAuthority(initial.state, {
    deploymentId: "candidate",
    healthUrl: "https://candidate.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  });
  assert.equal(replay.reused, true);
  assert.equal(replay.pending.epoch, 1);

  const aborted = abortPending(replay.state, {
    deploymentId: "candidate",
    deploymentEpoch: 1,
  });
  assert.equal(aborted.state.active, null);
  assert.equal(aborted.state.pending, null);
});

test("watchdog opens one incident at threshold and recovers cleanly", () => {
  let state = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-a1",
    healthUrl: "https://a1.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  }).state;
  state = commitAuthority(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  }).state;
  state = configureWatchdog(state, {
    enabled: true,
    intervalSeconds: 60,
    failureThreshold: 2,
  });

  let probe = applyProbe(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: false,
    detail: "timeout",
  });
  assert.equal(probe.shouldDispatch, false);

  probe = applyProbe(probe.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: false,
    detail: "timeout",
  });
  assert.equal(probe.shouldDispatch, true);
  const incidentId = probe.state.incident.id;

  state = markIncidentDispatched(probe.state, incidentId);
  probe = applyProbe(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: false,
    detail: "still down",
  });
  assert.equal(probe.shouldDispatch, false);
  assert.equal(probe.state.incident.id, incidentId);

  probe = applyProbe(probe.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: true,
  });
  assert.equal(probe.state.incident.status, "recovered");
  assert.equal(probe.state.active.consecutive_failures, 0);
});

test("probe results for a superseded deployment are ignored", () => {
  let state = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "new",
    healthUrl: "https://new.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  }).state;
  state = commitAuthority(state, {
    deploymentId: "new",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  }).state;

  const probe = applyProbe(state, {
    deploymentId: "old",
    deploymentEpoch: 0,
    healthy: false,
  });
  assert.equal(probe.ignored, true);
  assert.deepEqual(probe.state.active, state.active);
});


test("paid fallback carries expiry and is hard-fenced at expiry", () => {
  const now = Date.parse("2026-10-02T16:00:00Z");
  const expires = "2026-10-02T18:00:00.000Z";
  let state = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-paid",
    healthUrl: "https://paid.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
    costClass: "paid",
    hourlyEstimateUsd: 0.12,
    paidExpiresAt: expires,
    recoveryIncidentId: "incident-paid",
  }, now).state;
  state = commitAuthority(state, {
    deploymentId: "oci-paid",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  }, now + 1000).state;

  assert.equal(state.active.cost_class, "paid");
  assert.equal(state.active.paid_expires_at, expires);
  assert.equal(fenceResult(state, "oci-paid", 1, now + 60_000).authorized, true);

  const expired = fenceResult(state, "oci-paid", 1, Date.parse(expires));
  assert.equal(expired.authorized, false);
  assert.equal(expired.reason, "paid_fallback_expired");
});

test("malformed paid expiry fails closed", () => {
  const state = {
    ...defaultAuthorityState(),
    active: {
      deployment_id: "oci-paid",
      epoch: 4,
      cost_class: "paid",
      paid_expires_at: "not-a-date",
    },
  };
  const result = fenceResult(state, "oci-paid", 4, 1000);
  assert.equal(result.authorized, false);
  assert.equal(result.reason, "paid_fallback_expiry_invalid");
});

test("paid fallback opens one repatriation incident before hard expiry", () => {
  const now = Date.parse("2026-10-02T16:00:00Z");
  let state = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-paid",
    healthUrl: "https://paid.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
    costClass: "paid",
    hourlyEstimateUsd: 0.2,
    paidExpiresAt: "2026-10-02T17:00:00.000Z",
  }, now).state;
  state = commitAuthority(state, {
    deploymentId: "oci-paid",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  }, now + 1000).state;

  let result = evaluatePaidFallback(state, { leadSeconds: 1800 }, now);
  assert.equal(result.shouldDispatch, false);

  result = evaluatePaidFallback(
    state,
    { leadSeconds: 1800 },
    Date.parse("2026-10-02T16:31:00Z"),
  );
  assert.equal(result.shouldDispatch, true);
  assert.equal(result.state.incident.reason, "paid_fallback_repatriation");
  const incidentId = result.state.incident.id;

  const replay = evaluatePaidFallback(
    result.state,
    { leadSeconds: 1800 },
    Date.parse("2026-10-02T16:32:00Z"),
  );
  assert.equal(replay.state.incident.id, incidentId);
  assert.equal(replay.shouldDispatch, true);
});

test("healthy probes do not resolve a paid repatriation incident", () => {
  const state = {
    ...defaultAuthorityState(),
    active: {
      deployment_id: "oci-paid",
      epoch: 3,
      cost_class: "paid",
      paid_expires_at: "2026-10-02T17:00:00.000Z",
      consecutive_failures: 2,
    },
    incident: {
      id: "repatriate-1",
      reason: "paid_fallback_repatriation",
      status: "dispatched",
    },
  };
  const result = applyProbe(
    state,
    {
      deploymentId: "oci-paid",
      deploymentEpoch: 3,
      healthy: true,
    },
    Date.parse("2026-10-02T16:45:00Z"),
  );
  assert.equal(result.state.incident.status, "dispatched");
  assert.equal(result.state.active.consecutive_failures, 0);
});

test("retryable recovery outcome re-arms the same incident", () => {
  const state = {
    ...defaultAuthorityState(),
    incident: {
      id: "incident-1",
      reason: "health_probe_failure",
      status: "dispatched",
    },
  };
  const retry = recordRecoveryOutcome(state, {
    incidentId: "incident-1",
    outcome: "retryable",
    detail: "A1 host capacity unavailable",
  }, 1000);
  assert.equal(retry.incident.status, "pending_dispatch");
  assert.equal(retry.incident.id, "incident-1");
  assert.match(retry.incident.outcome_detail, /capacity unavailable/);
});

test("replacement commit resolves its recovery incident and records receipt", () => {
  const incident = {
    id: "incident-2",
    reason: "health_probe_failure",
    status: "dispatched",
  };
  let state = {
    ...defaultAuthorityState(),
    incident,
  };
  state = prepareAuthority(state, {
    deploymentId: "replacement",
    healthUrl: "https://replacement.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
    recoveryIncidentId: incident.id,
  }, 1000).state;
  state = commitAuthority(state, {
    deploymentId: "replacement",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  }, 2000).state;

  assert.equal(state.incident.status, "resolved");
  assert.equal(state.incident.last_outcome, "succeeded");
  assert.equal(state.last_recovery.incident_id, "incident-2");
  assert.equal(state.last_recovery.deployment_id, "replacement");
});
