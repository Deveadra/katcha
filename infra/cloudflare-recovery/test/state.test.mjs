import test from "node:test";
import assert from "node:assert/strict";

import {
  StateConflict,
  abortPending,
  applyProbe,
  commitAuthority,
  configureExternalCompute,
  configureWatchdog,
  configureBootstrapWatchdog,
  bootstrapWatchdogDecision,
  markBootstrapRedispatched,
  recordBootstrapHeartbeat,
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

test("bootstrap watchdog starts with a fresh grace window", () => {
  const now = Date.UTC(2026, 9, 5, 12, 0, 0);
  const state = configureBootstrapWatchdog(
    defaultAuthorityState(),
    {
      enabled: true,
      checkIntervalSeconds: 60,
      staleAfterSeconds: 900,
      redispatchCooldownSeconds: 1200,
    },
    now,
  );

  assert.equal(state.bootstrap_watchdog.enabled, true);
  assert.equal(state.bootstrap_watchdog.last_heartbeat_at_ms, now);
  assert.equal(
    bootstrapWatchdogDecision(state, now + 899_000).shouldDispatch,
    false,
  );
  const stale = bootstrapWatchdogDecision(state, now + 901_000);
  assert.equal(stale.shouldDispatch, true);
  assert.equal(stale.reason, "bootstrap heartbeat is stale");
});

test("bootstrap redispatch cooldown prevents outage dispatch storms", () => {
  const now = Date.UTC(2026, 9, 5, 12, 0, 0);
  let state = configureBootstrapWatchdog(
    defaultAuthorityState(),
    {
      enabled: true,
      checkIntervalSeconds: 60,
      staleAfterSeconds: 900,
      redispatchCooldownSeconds: 1200,
    },
    now,
  );

  state = markBootstrapRedispatched(state, now + 901_000);
  assert.equal(
    bootstrapWatchdogDecision(state, now + 1_500_000).shouldDispatch,
    false,
  );
  assert.equal(
    bootstrapWatchdogDecision(state, now + 2_102_000).shouldDispatch,
    true,
  );
});

test("bootstrap heartbeat resets staleness and terminal acquisition disables rescue", () => {
  const now = Date.UTC(2026, 9, 5, 12, 0, 0);
  let state = configureBootstrapWatchdog(
    defaultAuthorityState(),
    {
      enabled: true,
      checkIntervalSeconds: 60,
      staleAfterSeconds: 900,
      redispatchCooldownSeconds: 1200,
    },
    now,
  );

  state = recordBootstrapHeartbeat(
    state,
    { runId: "123", status: "started", terminal: false },
    now + 600_000,
  );
  assert.equal(
    bootstrapWatchdogDecision(state, now + 1_000_000).shouldDispatch,
    false,
  );
  assert.equal(state.bootstrap_watchdog.last_run_id, "123");

  state = recordBootstrapHeartbeat(
    state,
    { runId: "123", status: "acquired", terminal: true },
    now + 1_100_000,
  );
  assert.equal(state.bootstrap_watchdog.enabled, false);
  assert.equal(
    bootstrapWatchdogDecision(state, now + 10_000_000).shouldDispatch,
    false,
  );
});

test("watchdog defaults unresolved recovery retry to five minutes", () => {
  const state = defaultAuthorityState();
  assert.equal(state.watchdog.recovery_retry_seconds, 300);

  const configured = configureWatchdog(state, {
    enabled: true,
    intervalSeconds: 60,
    failureThreshold: 3,
  });
  assert.equal(configured.watchdog.recovery_retry_seconds, 300);
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
    recoveryRetrySeconds: 300,
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
  }, 5_000 + 299_000);
  assert.equal(probe.shouldDispatch, false);
  assert.equal(probe.state.incident.status, "dispatched");

  probe = applyProbe(probe.state, {
    deploymentId: "oci-a1",
    deploymentEpoch: 1,
    healthy: false,
    detail: "still down",
  }, 5_000 + 300_000);
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


test("kill switch denies replay of an existing active reservation", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const first = reserveExternalCompute(
    configuredComputeState(now),
    {
      jobKey: "kill-switch-replay",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-kill-switch",
      attempt: 1,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 3600,
    },
    now,
  );
  const disabled = configureExternalCompute(
    first.state,
    {
      enabled: false,
      monthlyLimitMicrousd: 5_000_000,
      providerLimitsMicrousd: {
        oci: 3_000_000,
        modal: 2_000_000,
      },
      maxConcurrentJobs: 2,
      maxRetrySpendMicrousd: 2_500_000,
    },
    now + 1000,
  );

  assert.throws(
    () =>
      reserveExternalCompute(
        disabled,
        {
          jobKey: "kill-switch-replay",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-kill-switch",
          attempt: 1,
          estimatedCostMicrousd: 500_000,
          ttlSeconds: 3600,
        },
        now + 2000,
      ),
    /external compute is disabled/,
  );
});

test("settled spend cannot be evicted by reservation history growth", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  let state = configureExternalCompute(
    defaultAuthorityState(),
    {
      enabled: true,
      monthlyLimitMicrousd: 550_000,
      providerLimitsMicrousd: { modal: 550_000 },
      maxConcurrentJobs: 2,
      maxRetrySpendMicrousd: 10_000,
    },
    now,
  );

  for (let index = 0; index < 550; index += 1) {
    const reserved = reserveExternalCompute(
      state,
      {
        jobKey: `history-${index}`,
        provider: "modal",
        operation: "tiny-job",
        retryGroup: `group-${index}`,
        attempt: 1,
        estimatedCostMicrousd: 1_000,
        ttlSeconds: 3600,
      },
      now + index * 2,
    );
    state = settleExternalCompute(
      reserved.state,
      {
        reservationId: reserved.reservation.id,
        actualCostMicrousd: 1_000,
      },
      now + index * 2 + 1,
    ).state;
  }

  const status = externalComputeStatus(state, now + 2000).status;
  assert.equal(status.settled_microusd, 550_000);
  assert.equal(status.provider_settled_microusd.modal, 550_000);
  assert.equal(state.external_compute.reservations.length, 550);

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "history-over-budget",
          provider: "modal",
          operation: "tiny-job",
          retryGroup: "new-group",
          attempt: 1,
          estimatedCostMicrousd: 1,
          ttlSeconds: 3600,
        },
        now + 3000,
      ),
    /monthly budget exceeded/,
  );
});

test("retry-group settled spend survives UTC month rollover", () => {
  const january = Date.UTC(2026, 0, 31, 23, 50, 0);
  let state = configuredComputeState(january);
  const first = reserveExternalCompute(
    state,
    {
      jobKey: "cross-month-attempt-1",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-cross-month",
      attempt: 1,
      estimatedCostMicrousd: 1_200_000,
      ttlSeconds: 3600,
    },
    january,
  );
  state = settleExternalCompute(
    first.state,
    {
      reservationId: first.reservation.id,
      actualCostMicrousd: 1_000_000,
    },
    january + 1000,
  ).state;

  const february = Date.UTC(2026, 1, 1, 0, 10, 0);
  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "cross-month-attempt-2",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-cross-month",
          attempt: 2,
          estimatedCostMicrousd: 1_600_000,
          ttlSeconds: 3600,
        },
        february,
      ),
    /retry budget exceeded/,
  );
});

test("legacy settled reservations migrate into durable spend aggregates", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const legacy = {
    ...defaultAuthorityState(),
    external_compute: {
      enabled: true,
      month_key: "2026-10",
      monthly_limit_microusd: 5_000_000,
      provider_limits_microusd: { oci: 3_000_000 },
      max_concurrent_jobs: 2,
      max_retry_spend_microusd: 2_500_000,
      reservations: [
        {
          id: "legacy-settled",
          job_key: "legacy-job",
          provider: "oci",
          operation: "paid-fallback",
          retry_group: "legacy-incident",
          attempt: 1,
          estimated_cost_microusd: 500_000,
          actual_cost_microusd: 400_000,
          status: "settled",
          reserved_at: new Date(now - 1000).toISOString(),
          expires_at: new Date(now + 1000).toISOString(),
          expires_at_ms: now + 1000,
          settled_at: new Date(now).toISOString(),
          metadata: {},
        },
      ],
    },
  };

  const status = externalComputeStatus(legacy, now).status;
  assert.equal(status.settled_microusd, 400_000);
  assert.equal(status.provider_settled_microusd.oci, 400_000);

  assert.throws(
    () =>
      reserveExternalCompute(
        configureExternalCompute(
          legacy,
          {
            enabled: true,
            monthlyLimitMicrousd: 450_000,
            providerLimitsMicrousd: { oci: 450_000 },
            maxConcurrentJobs: 2,
            maxRetrySpendMicrousd: 2_500_000,
          },
          now,
        ),
        {
          jobKey: "legacy-over-budget",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "new-incident",
          attempt: 1,
          estimatedCostMicrousd: 100_000,
          ttlSeconds: 3600,
        },
        now + 1000,
      ),
    /monthly budget exceeded/,
  );
});


test("retry groups open their circuit after the configured attempt ceiling", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  let state = configureExternalCompute(
    defaultAuthorityState(),
    {
      enabled: true,
      monthlyLimitMicrousd: 5_000_000,
      providerLimitsMicrousd: { oci: 5_000_000 },
      maxConcurrentJobs: 2,
      maxRetryAttempts: 2,
      maxRetrySpendMicrousd: 5_000_000,
    },
    now,
  );

  for (const attempt of [1, 2]) {
    const reserved = reserveExternalCompute(
      state,
      {
        jobKey: `attempt-limit-${attempt}`,
        provider: "oci",
        operation: "paid-fallback",
        retryGroup: "incident-attempt-limit",
        attempt,
        estimatedCostMicrousd: 100_000,
        ttlSeconds: 3600,
      },
      now + attempt * 10,
    );
    state = releaseExternalCompute(
      reserved.state,
      {
        reservationId: reserved.reservation.id,
        reason: "provider unavailable before launch",
      },
      now + attempt * 10 + 1,
    ).state;
  }

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "attempt-limit-3",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-attempt-limit",
          attempt: 3,
          estimatedCostMicrousd: 100_000,
          ttlSeconds: 3600,
        },
        now + 100,
      ),
    /retry attempt limit reached/,
  );
});


test("retry-group ledger fails closed instead of evicting spend history", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const configured = configuredComputeState(now);
  const retrySettled = Object.fromEntries(
    Array.from({ length: 10000 }, (_, index) => [
      `historical-group-${index}`,
      1,
    ]),
  );
  const state = {
    ...configured,
    external_compute: {
      ...configured.external_compute,
      retry_settled_microusd: retrySettled,
    },
  };

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "new-group-job",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "brand-new-group",
          attempt: 1,
          estimatedCostMicrousd: 1,
          ttlSeconds: 3600,
        },
        now + 1000,
      ),
    /retry-group state safety limit reached/,
  );
});


test("retry attempt ceiling survives UTC month rollover", () => {
  const january = Date.UTC(2026, 0, 31, 23, 50, 0);
  let state = configureExternalCompute(
    defaultAuthorityState(),
    {
      enabled: true,
      monthlyLimitMicrousd: 5_000_000,
      providerLimitsMicrousd: { oci: 5_000_000 },
      maxConcurrentJobs: 2,
      maxRetryAttempts: 1,
      maxRetrySpendMicrousd: 5_000_000,
    },
    january,
  );
  const first = reserveExternalCompute(
    state,
    {
      jobKey: "month-attempt-1",
      provider: "oci",
      operation: "paid-fallback",
      retryGroup: "incident-month-attempt",
      attempt: 1,
      estimatedCostMicrousd: 100_000,
      ttlSeconds: 3600,
    },
    january,
  );
  state = settleExternalCompute(
    first.state,
    {
      reservationId: first.reservation.id,
      actualCostMicrousd: 50_000,
    },
    january + 1000,
  ).state;

  const february = Date.UTC(2026, 1, 1, 0, 10, 0);
  const status = externalComputeStatus(state, february).status;
  assert.equal(status.retry_attempts["incident-month-attempt"], 1);

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "month-attempt-2",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "incident-month-attempt",
          attempt: 2,
          estimatedCostMicrousd: 100_000,
          ttlSeconds: 3600,
        },
        february,
      ),
    /retry attempt limit reached/,
  );
});

test("legacy reservation history migrates retry attempt counters", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const legacy = {
    ...defaultAuthorityState(),
    external_compute: {
      enabled: true,
      month_key: "2026-10",
      monthly_limit_microusd: 5_000_000,
      provider_limits_microusd: { oci: 5_000_000 },
      max_concurrent_jobs: 2,
      max_retry_attempts: 3,
      max_retry_spend_microusd: 5_000_000,
      reservations: [
        {
          id: "legacy-attempt-two",
          job_key: "legacy-attempt-two",
          provider: "oci",
          operation: "paid-fallback",
          retry_group: "legacy-attempt-group",
          attempt: 2,
          estimated_cost_microusd: 100_000,
          actual_cost_microusd: 50_000,
          status: "settled",
          reserved_at: new Date(now - 2000).toISOString(),
          expires_at: new Date(now + 1000).toISOString(),
          expires_at_ms: now + 1000,
          settled_at: new Date(now - 1000).toISOString(),
          metadata: {},
        },
      ],
    },
  };

  const status = externalComputeStatus(legacy, now).status;
  assert.equal(status.retry_attempts["legacy-attempt-group"], 2);

  assert.throws(
    () =>
      reserveExternalCompute(
        legacy,
        {
          jobKey: "legacy-repeat-two",
          provider: "oci",
          operation: "paid-fallback",
          retryGroup: "legacy-attempt-group",
          attempt: 2,
          estimatedCostMicrousd: 100_000,
          ttlSeconds: 3600,
        },
        now + 1000,
      ),
    /retry attempt limit reached/,
  );
});



test("external compute atomically allocates retry attempts when omitted", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  let state = configureExternalCompute(
    defaultAuthorityState(),
    {
      enabled: true,
      monthlyLimitMicrousd: 5_000_000,
      providerLimitsMicrousd: { "aws-lambda": 5_000_000 },
      maxConcurrentJobs: 2,
      maxRetryAttempts: 2,
      maxRetrySpendMicrousd: 5_000_000,
    },
    now,
  );

  const first = reserveExternalCompute(
    state,
    {
      jobKey: "render-job-1",
      provider: "aws-lambda",
      operation: "remotion-video-render",
      retryGroup: "render-output-1",
      attempt: null,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 3600,
    },
    now + 1,
  );
  assert.equal(first.reservation.attempt, 1);
  state = releaseExternalCompute(
    first.state,
    {
      reservationId: first.reservation.id,
      reason: "test retry",
    },
    now + 2,
  ).state;

  const second = reserveExternalCompute(
    state,
    {
      jobKey: "render-job-2",
      provider: "aws-lambda",
      operation: "remotion-video-render",
      retryGroup: "render-output-1",
      attempt: null,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 3600,
    },
    now + 3,
  );
  assert.equal(second.reservation.attempt, 2);
  state = releaseExternalCompute(
    second.state,
    {
      reservationId: second.reservation.id,
      reason: "test retry",
    },
    now + 4,
  ).state;

  assert.throws(
    () =>
      reserveExternalCompute(
        state,
        {
          jobKey: "render-job-3",
          provider: "aws-lambda",
          operation: "remotion-video-render",
          retryGroup: "render-output-1",
          attempt: null,
          estimatedCostMicrousd: 500_000,
          ttlSeconds: 3600,
        },
        now + 5,
      ),
    /retry attempt limit reached/,
  );
});



test("finite external compute reservation settles its ceiling on expiry", () => {
  const now = Date.UTC(2026, 9, 4, 12, 0, 0);
  const state = configureExternalCompute(
    defaultAuthorityState(),
    {
      enabled: true,
      monthlyLimitMicrousd: 5_000_000,
      providerLimitsMicrousd: { "aws-lambda": 5_000_000 },
      maxConcurrentJobs: 2,
      maxRetryAttempts: 3,
      maxRetrySpendMicrousd: 5_000_000,
    },
    now,
  );
  const reserved = reserveExternalCompute(
    state,
    {
      jobKey: "finite-render",
      provider: "aws-lambda",
      operation: "remotion-video-render",
      retryGroup: "render-output",
      attempt: null,
      estimatedCostMicrousd: 500_000,
      ttlSeconds: 300,
      settleOnExpiry: true,
    },
    now,
  );

  const status = externalComputeStatus(
    reserved.state,
    now + 301_000,
  ).status;
  const row = status.reservations.find(
    (item) => item.id === reserved.reservation.id,
  );

  assert.equal(row.status, "settled");
  assert.equal(row.actual_cost_microusd, 500_000);
  assert.equal(row.metadata.auto_settled_on_expiry, true);
  assert.equal(status.reserved_microusd, 0);
  assert.equal(status.expired_held_microusd, 0);
  assert.equal(status.settled_microusd, 500_000);
  assert.equal(status.provider_settled_microusd["aws-lambda"], 500_000);
});
