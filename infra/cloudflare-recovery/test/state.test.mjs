import test from "node:test";
import assert from "node:assert/strict";

import {
  StateConflict,
  abortPending,
  applyProbe,
  commitAuthority,
  configureExternalCompute,
  configureWatchdog,
  defaultAuthorityState,
  externalComputeStatus,
  fenceResult,
  markCandidateReady,
  markIncidentDispatched,
  prepareAuthority,
  releaseExternalCompute,
  reserveExternalCompute,
  settleExternalCompute,
} from "../src/state.mjs";

test("prepare allocates a monotonic epoch without fencing the active leader", () => {
  const first = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-a1",
    healthUrl: "https://a1.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  }, 1000);
  assert.equal(first.pending.epoch, 1);
  assert.equal(first.state.active, null);

  const ready = markCandidateReady(first.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    readiness: {
      runtime_ready: true,
      durable_state_ready: true,
      fence_probe_ready: true,
      public_route_ready: true,
    },
  }, 1500);
  const committed = commitAuthority(ready.state, {
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
  const ready = markCandidateReady(prepared.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    readiness: {
      runtime_ready: true,
      durable_state_ready: true,
      fence_probe_ready: true,
      public_route_ready: true,
    },
  });
  const committed = commitAuthority(ready.state, {
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
  const ready = markCandidateReady(prepared.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    readiness: {
      runtime_ready: true,
      durable_state_ready: true,
      fence_probe_ready: true,
      public_route_ready: true,
    },
  });
  const committed = commitAuthority(ready.state, {
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
  state = markCandidateReady(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    readiness: {
      runtime_ready: true,
      durable_state_ready: true,
      fence_probe_ready: true,
      public_route_ready: true,
    },
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
  state = markCandidateReady(state, {
    deploymentId: "new",
    deploymentEpoch: 1,
    readiness: {
      runtime_ready: true,
      durable_state_ready: true,
      fence_probe_ready: true,
      public_route_ready: true,
    },
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


test("commit refuses a candidate that has not proven readiness", () => {
  const prepared = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "candidate",
    healthUrl: "https://candidate.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  });

  assert.throws(
    () => commitAuthority(prepared.state, {
      deploymentId: "candidate",
      deploymentEpoch: 1,
      expectedActiveEpoch: 0,
    }),
    /has not passed candidate readiness checks/,
  );
});

test("candidate readiness requires all mandatory checks", () => {
  const prepared = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "candidate",
    healthUrl: "https://candidate.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  });

  assert.throws(
    () => markCandidateReady(prepared.state, {
      deploymentId: "candidate",
      deploymentEpoch: 1,
      readiness: {
        runtime_ready: true,
        durable_state_ready: true,
        fence_probe_ready: false,
        public_route_ready: true,
      },
    }),
    /fence_probe_ready/,
  );

  const ready = markCandidateReady(prepared.state, {
    deploymentId: "candidate",
    deploymentEpoch: 1,
    readiness: {
      runtime_ready: true,
      durable_state_ready: true,
      fence_probe_ready: true,
      public_route_ready: true,
      detail: "local acceptance passed",
    },
  });
  assert.ok(ready.pending.ready_at);
  assert.equal(ready.pending.readiness.detail, "local acceptance passed");
});


test("unresolved dispatched incident is re-queued after retry interval", () => {
  let state = prepareAuthority(defaultAuthorityState(), {
    deploymentId: "oci-a1",
    healthUrl: "https://a1.example.test/v1/health/ready",
    expectedActiveEpoch: 0,
  }, 0).state;
  state = markCandidateReady(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    readiness: {
      runtime_ready: true,
      durable_state_ready: true,
      fence_probe_ready: true,
      public_route_ready: true,
    },
  }, 1000).state;
  state = commitAuthority(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    expectedActiveEpoch: 0,
  }, 2000).state;
  state = configureWatchdog(state, {
    enabled: true,
    intervalSeconds: 60,
    failureThreshold: 1,
    recoveryRetrySeconds: 900,
  }, 3000);

  let probe = applyProbe(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: false,
    detail: "down",
  }, 4000);
  assert.equal(probe.shouldDispatch, true);
  const incidentId = probe.state.incident.id;
  state = markIncidentDispatched(probe.state, incidentId, 5000);

  probe = applyProbe(state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: false,
    detail: "still down",
  }, 5_000 + 899_000);
  assert.equal(probe.shouldDispatch, false);
  assert.equal(probe.state.incident.status, "dispatched");

  probe = applyProbe(probe.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: false,
    detail: "still down",
  }, 5_000 + 900_000);
  assert.equal(probe.shouldDispatch, true);
  assert.equal(probe.state.incident.status, "pending_dispatch");
  assert.equal(probe.state.incident.id, incidentId);
});


function configuredComputeState(nowMs = Date.UTC(2026, 9, 4, 12, 0, 0)) {
  return configureExternalCompute(
    defaultAuthorityState(),
    {
      enabled: true,
      monthlyLimitMicrousd: 5_000_000,
      providerLimitsMicrousd: {
        oci: 3_000_000,
        modal: 2_000_000,
      },
      maxConcurrentJobs: 2,
      maxRetrySpendMicrousd: 2_500_000,
    },
    nowMs,
  );
}

test("external compute is denied while the global kill switch is off", () => {
  assert.throws(
    () =>
      reserveExternalCompute(
        defaultAuthorityState(),
        {
          jobKey: "job-1",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-1",
          attempt: 1,
          estimatedCostMicrousd: 500_000,
          ttlSeconds: 3600,
        },
        Date.UTC(2026, 9, 4, 12, 0, 0),
      ),
    /external compute is disabled/,
  );
});

test("external compute reservation is idempotent by job key", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const first = reserveExternalCompute(
    configuredComputeState(now),
    {
      jobKey: "incident-1:oci:ad1",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-1",
      attempt: 1,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 3600,
    },
    now,
  );
  const replay = reserveExternalCompute(
    first.state,
    {
      jobKey: "incident-1:oci:ad1",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-1",
      attempt: 1,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 3600,
    },
    now + 1000,
  );

  assert.equal(replay.reused, true);
  assert.equal(replay.reservation.id, first.reservation.id);
});

test("monthly and provider ceilings deny projected overspend", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  let state = configuredComputeState(now);
  state = reserveExternalCompute(
    state,
    {
      jobKey: "modal-1",
      provider: "modal",
      operation: "render",
      retryGroup: "render-1",
      attempt: 1,
      estimatedCostMicrousd: 1_800_000,
      ttlSeconds: 3600,
    },
    now,
  ).state;

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "modal-2",
          provider: "modal",
          operation: "render",
          retryGroup: "render-2",
          attempt: 1,
          estimatedCostMicrousd: 300_000,
          ttlSeconds: 3600,
        },
        now + 1000,
      ),
    /provider budget exceeded/,
  );

  const capped = configureExternalCompute(
    state,
    {
      enabled: true,
      monthlyLimitMicrousd: 1_900_000,
      providerLimitsMicrousd: { oci: 3_000_000, modal: 3_000_000 },
      maxConcurrentJobs: 3,
      maxRetrySpendMicrousd: 2_500_000,
    },
    now + 2000,
  );
  assert.throws(
    () =>
      reserveExternalCompute(
        capped,
        {
          jobKey: "oci-1",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-1",
          attempt: 1,
          estimatedCostMicrousd: 200_000,
          ttlSeconds: 3600,
        },
        now + 3000,
      ),
    /monthly budget exceeded/,
  );
});

test("external compute enforces concurrent job limit", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  let state = configuredComputeState(now);
  for (const index of [1, 2]) {
    state = reserveExternalCompute(
      state,
      {
        jobKey: `job-${index}`,
        provider: "oci",
        operation: "paid-fallback",
        retryGroup: `incident-${index}`,
        attempt: 1,
        estimatedCostMicrousd: 250_000,
        ttlSeconds: 3600,
      },
      now + index,
    ).state;
  }

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "job-3",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-3",
          attempt: 1,
          estimatedCostMicrousd: 250_000,
          ttlSeconds: 3600,
        },
        now + 10,
      ),
    /concurrency limit reached/,
  );
});

test("retry-group spend includes settled and reserved attempts", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  let first = reserveExternalCompute(
    configuredComputeState(now),
    {
      jobKey: "incident-1:attempt-1",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-1",
      attempt: 1,
      estimatedCostMicrousd: 1_200_000,
      ttlSeconds: 3600,
    },
    now,
  );
  let state = settleExternalCompute(
    first.state,
    {
      reservationId: first.reservation.id,
      actualCostMicrousd: 1_000_000,
    },
    now + 1000,
  ).state;
  state = reserveExternalCompute(
    state,
    {
      jobKey: "incident-1:attempt-2",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-1",
      attempt: 2,
      estimatedCostMicrousd: 1_200_000,
      ttlSeconds: 3600,
    },
    now + 2000,
  ).state;

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "incident-1:attempt-3",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-1",
          attempt: 3,
          estimatedCostMicrousd: 400_000,
          ttlSeconds: 3600,
        },
        now + 3000,
      ),
    /retry budget exceeded/,
  );
});

test("released reservation frees concurrency and budget headroom", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const first = reserveExternalCompute(
    configuredComputeState(now),
    {
      jobKey: "job-release",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-release",
      attempt: 1,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 3600,
    },
    now,
  );
  const released = releaseExternalCompute(
    first.state,
    {
      reservationId: first.reservation.id,
      reason: "provider job was never launched",
    },
    now + 1000,
  );
  const status = externalComputeStatus(released.state, now + 1000).status;

  assert.equal(status.concurrent_jobs, 0);
  assert.equal(status.reserved_microusd, 0);
});

test("expired reservation can still settle actual provider cost", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const first = reserveExternalCompute(
    configuredComputeState(now),
    {
      jobKey: "job-expired",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-expired",
      attempt: 1,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 60,
    },
    now,
  );
  const settled = settleExternalCompute(
    first.state,
    {
      reservationId: first.reservation.id,
      actualCostMicrousd: 125_000,
    },
    now + 61_000,
  );
  assert.equal(settled.reservation.status, "settled");
  assert.equal(settled.reservation.actual_cost_microusd, 125_000);
});

test("UTC month rollover drops old settled spend but carries active reservations", () => {
  const january = Date.UTC(2026, 0, 31, 23, 50, 0);
  let first = reserveExternalCompute(
    configuredComputeState(january),
    {
      jobKey: "active-over-month",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-month",
      attempt: 1,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 7200,
    },
    january,
  );
  const settled = reserveExternalCompute(
    first.state,
    {
      jobKey: "settled-january",
      provider: "modal",
      operation: "render",
      retryGroup: "render-january",
      attempt: 1,
      estimatedCostMicrousd: 300_000,
      ttlSeconds: 3600,
    },
    january + 1000,
  );
  first = {
    ...first,
    state: settleExternalCompute(
      settled.state,
      {
        reservationId: settled.reservation.id,
        actualCostMicrousd: 250_000,
      },
      january + 2000,
    ).state,
  };

  const february = Date.UTC(2026, 1, 1, 0, 10, 0);
  const status = externalComputeStatus(first.state, february).status;

  assert.equal(status.month_key, "2026-02");
  assert.equal(status.settled_microusd, 0);
  assert.equal(status.reserved_microusd, 500_000);
  assert.equal(status.concurrent_jobs, 1);
});
