export class StateConflict extends Error {
  constructor(message, status = 409) {
    super(message);
    this.name = "StateConflict";
    this.status = status;
  }
}

export function defaultAuthorityState() {
  return {
    version: 1,
    max_epoch: 0,
    active: null,
    pending: null,
    watchdog: {
      enabled: false,
      interval_seconds: 60,
      failure_threshold: 3,
      recovery_retry_seconds: 900,
    },
    incident: null,
    events: [],
  };
}

function nowIso(nowMs) {
  return new Date(nowMs).toISOString();
}

function event(state, type, nowMs, details = {}) {
  const events = [
    ...(Array.isArray(state.events) ? state.events : []),
    { type, at: nowIso(nowMs), ...details },
  ].slice(-100);
  return { ...state, events };
}

export function normalizeState(value) {
  const base = defaultAuthorityState();
  if (!value || typeof value !== "object") return base;
  return {
    ...base,
    ...value,
    watchdog: { ...base.watchdog, ...(value.watchdog || {}) },
    events: Array.isArray(value.events) ? value.events.slice(-100) : [],
  };
}

export function prepareAuthority(
  current,
  { deploymentId, healthUrl, expectedActiveEpoch },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  const activeEpoch = state.active?.epoch ?? 0;
  if (activeEpoch !== expectedActiveEpoch) {
    throw new StateConflict(
      `active epoch changed: expected ${expectedActiveEpoch}, found ${activeEpoch}`,
    );
  }
  if (state.pending) {
    if (
      state.pending.deployment_id === deploymentId &&
      state.pending.health_url === healthUrl
    ) {
      return { state, pending: state.pending, reused: true };
    }
    throw new StateConflict(
      `another deployment is already pending: ${state.pending.deployment_id}@${state.pending.epoch}`,
    );
  }

  const epoch = Math.max(state.max_epoch || 0, activeEpoch) + 1;
  const pending = {
    deployment_id: deploymentId,
    epoch,
    health_url: healthUrl,
    prepared_at: nowIso(nowMs),
    ready_at: null,
    readiness: null,
  };
  let next = {
    ...state,
    max_epoch: epoch,
    pending,
  };
  next = event(next, "authority.prepared", nowMs, {
    deployment_id: deploymentId,
    epoch,
    expected_active_epoch: expectedActiveEpoch,
  });
  return { state: next, pending, reused: false };
}

export function markCandidateReady(
  current,
  { deploymentId, deploymentEpoch, readiness },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  const pending = state.pending;
  if (
    !pending ||
    pending.deployment_id !== deploymentId ||
    pending.epoch !== deploymentEpoch
  ) {
    throw new StateConflict("pending authority does not match candidate readiness");
  }
  const checks = readiness && typeof readiness === "object" ? readiness : {};
  for (const name of [
    "runtime_ready",
    "durable_state_ready",
    "fence_probe_ready",
    "public_route_ready",
  ]) {
    if (checks[name] !== true) {
      throw new StateConflict(`candidate readiness check failed: ${name}`, 422);
    }
  }
  const readyAt = nowIso(nowMs);
  const nextPending = {
    ...pending,
    ready_at: readyAt,
    readiness: checks,
  };
  let next = { ...state, pending: nextPending };
  next = event(next, "authority.candidate_ready", nowMs, {
    deployment_id: deploymentId,
    epoch: deploymentEpoch,
  });
  return { state: next, pending: nextPending };
}


export function commitAuthority(
  current,
  { deploymentId, deploymentEpoch, expectedActiveEpoch },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  const activeEpoch = state.active?.epoch ?? 0;
  if (activeEpoch !== expectedActiveEpoch) {
    throw new StateConflict(
      `active epoch changed: expected ${expectedActiveEpoch}, found ${activeEpoch}`,
    );
  }
  const pending = state.pending;
  if (
    !pending ||
    pending.deployment_id !== deploymentId ||
    pending.epoch !== deploymentEpoch
  ) {
    throw new StateConflict("pending authority does not match the requested commit");
  }
  if (!pending.ready_at) {
    throw new StateConflict(
      "pending deployment has not passed candidate readiness checks",
      422,
    );
  }

  const active = {
    deployment_id: pending.deployment_id,
    epoch: pending.epoch,
    health_url: pending.health_url,
    committed_at: nowIso(nowMs),
    last_probe_at: null,
    last_healthy_at: null,
    consecutive_failures: 0,
  };
  let next = {
    ...state,
    active,
    pending: null,
    incident: null,
  };
  next = event(next, "authority.committed", nowMs, {
    deployment_id: active.deployment_id,
    epoch: active.epoch,
    previous_epoch: activeEpoch,
  });
  return { state: next, active };
}

export function abortPending(
  current,
  { deploymentId, deploymentEpoch },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  if (!state.pending) {
    return { state, aborted: false };
  }
  if (
    state.pending.deployment_id !== deploymentId ||
    state.pending.epoch !== deploymentEpoch
  ) {
    throw new StateConflict("pending authority does not match the requested abort");
  }
  let next = { ...state, pending: null };
  next = event(next, "authority.aborted", nowMs, {
    deployment_id: deploymentId,
    epoch: deploymentEpoch,
  });
  return { state: next, aborted: true };
}

export function configureWatchdog(
  current,
  { enabled, intervalSeconds, failureThreshold, recoveryRetrySeconds },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  let next = {
    ...state,
    watchdog: {
      enabled,
      interval_seconds: intervalSeconds,
      failure_threshold: failureThreshold,
    recovery_retry_seconds: recoveryRetrySeconds,
      recovery_retry_seconds: recoveryRetrySeconds,
    },
  };
  next = event(next, "watchdog.configured", nowMs, {
    enabled,
    interval_seconds: intervalSeconds,
    failure_threshold: failureThreshold,
  });
  return next;
}

export function applyProbe(
  current,
  { deploymentId, deploymentEpoch, healthy, detail = "" },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  const active = state.active;
  if (
    !active ||
    active.deployment_id !== deploymentId ||
    active.epoch !== deploymentEpoch
  ) {
    return { state, ignored: true, shouldDispatch: false };
  }

  const probeAt = nowIso(nowMs);
  if (healthy) {
    let incident = state.incident;
    if (incident && !["recovered", "resolved"].includes(incident.status)) {
      incident = {
        ...incident,
        status: "recovered",
        recovered_at: probeAt,
      };
    }
    let next = {
      ...state,
      active: {
        ...active,
        last_probe_at: probeAt,
        last_healthy_at: probeAt,
        consecutive_failures: 0,
      },
      incident,
    };
    next = event(next, "watchdog.healthy", nowMs, {
      deployment_id: deploymentId,
      epoch: deploymentEpoch,
    });
    return { state: next, ignored: false, shouldDispatch: false };
  }

  const failures = Number(active.consecutive_failures || 0) + 1;
  let incident = state.incident;
  let shouldDispatch = false;
  if (
    failures >= state.watchdog.failure_threshold &&
    (!incident || ["recovered", "resolved"].includes(incident.status))
  ) {
    incident = {
      id: crypto.randomUUID(),
      status: "pending_dispatch",
      opened_at: probeAt,
      deployment_id: deploymentId,
      epoch: deploymentEpoch,
      failure_count: failures,
      last_error: String(detail || "health probe failed").slice(0, 1000),
    };
    shouldDispatch = true;
  } else if (incident && !["recovered", "resolved"].includes(incident.status)) {
    incident = {
      ...incident,
      failure_count: failures,
      last_error: String(detail || "health probe failed").slice(0, 1000),
    };
    if (
      incident.status === "dispatched" &&
      incident.dispatched_at &&
      nowMs - Date.parse(incident.dispatched_at) >=
        state.watchdog.recovery_retry_seconds * 1000
    ) {
      incident = {
        ...incident,
        status: "pending_dispatch",
        retry_queued_at: probeAt,
      };
      shouldDispatch = true;
    }
  }

  let next = {
    ...state,
    active: {
      ...active,
      last_probe_at: probeAt,
      consecutive_failures: failures,
    },
    incident,
  };
  next = event(next, "watchdog.unhealthy", nowMs, {
    deployment_id: deploymentId,
    epoch: deploymentEpoch,
    failure_count: failures,
  });
  return { state: next, ignored: false, shouldDispatch };
}

export function markIncidentDispatched(current, incidentId, nowMs = Date.now()) {
  const state = normalizeState(current);
  if (!state.incident || state.incident.id !== incidentId) return state;
  if (state.incident.status !== "pending_dispatch") return state;
  let next = {
    ...state,
    incident: {
      ...state.incident,
      status: "dispatched",
      dispatched_at: nowIso(nowMs),
      last_dispatch_error: null,
    },
  };
  next = event(next, "recovery.dispatched", nowMs, { incident_id: incidentId });
  return next;
}

export function markIncidentDispatchFailed(
  current,
  incidentId,
  error,
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  if (!state.incident || state.incident.id !== incidentId) return state;
  let next = {
    ...state,
    incident: {
      ...state.incident,
      status: "pending_dispatch",
      last_dispatch_error: String(error || "dispatch failed").slice(0, 1000),
      last_dispatch_attempt_at: nowIso(nowMs),
    },
  };
  next = event(next, "recovery.dispatch_failed", nowMs, {
    incident_id: incidentId,
  });
  return next;
}

export function fenceResult(current, deploymentId, deploymentEpoch) {
  const state = normalizeState(current);
  const active = state.active;
  return {
    authorized:
      Boolean(active) &&
      active.deployment_id === deploymentId &&
      active.epoch === deploymentEpoch,
    active_epoch: active?.epoch ?? 0,
    leader_id: active?.deployment_id ?? "",
  };
}
