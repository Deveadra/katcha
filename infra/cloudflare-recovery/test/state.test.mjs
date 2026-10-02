import test from "node:test";
import assert from "node:assert/strict";

import {
  StateConflict,
  abortPending,
  applyProbe,
  commitAuthority,
  configureWatchdog,
  defaultAuthorityState,
  fenceResult,
  markCandidateReady,
  markIncidentDispatched,
  prepareAuthority,
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
      detail: "local acceptance passed",
    },
  });
  assert.ok(ready.pending.ready_at);
  assert.equal(ready.pending.readiness.detail, "local acceptance passed");
});
