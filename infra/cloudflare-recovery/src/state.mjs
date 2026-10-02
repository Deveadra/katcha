export class StateConflict extends Error {
  constructor(message, status = 409) {
    super(message);
    this.name = "StateConflict";
    this.status = status;
  }
}

export function defaultAuthorityState() {
  return {
    version: 2,
    max_epoch: 0,
    active: null,
    pending: null,
    watchdog: {
      enabled: false,
      interval_seconds: 60,
      failure_threshold: 3,
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
  {
    deploymentId,
    healthUrl,
    expectedActiveEpoch,
    costClass = "always_free",
    hourlyEstimateUsd = null,
    paidExpiresAt = null,
    recoveryIncidentId = null,
  },
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
      state.pending.health_url === healthUrl &&
      state.pending.cost_class === costClass &&
      state.pending.hourly_estimate_usd === hourlyEstimateUsd &&
      state.pending.paid_expires_at === paidExpiresAt &&
      state.pending.recovery_incident_id === recoveryIncidentId
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
    cost_class: costClass,
    hourly_estimate_usd: hourlyEstimateUsd,
    paid_expires_at: paidExpiresAt,
    recovery_incident_id: recoveryIncidentId,
    prepared_at: nowIso(nowMs),
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

  const active = {
    deployment_id: pending.deployment_id,
    epoch: pending.epoch,
    health_url: pending.health_url,
    cost_class: pending.cost_class || "always_free",
    hourly_estimate_usd: pending.hourly_estimate_usd ?? null,
    paid_expires_at: pending.paid_expires_at ?? null,
    recovery_incident_id: pending.recovery_incident_id ?? null,
    committed_at: nowIso(nowMs),
    last_probe_at: null,
    last_healthy_at: null,
    consecutive_failures: 0,
  };
  const resolvesIncident =
    Boolean(pending.recovery_incident_id) &&
    state.incident?.id === pending.recovery_incident_id;
  const incident = resolvesIncident
    ? {
        ...state.incident,
        status: "resolved",
        resolved_at: nowIso(nowMs),
        last_outcome: "succeeded",
        last_outcome_at: nowIso(nowMs),
        outcome_detail: "replacement authority committed",
      }
    : state.incident;
  const lastRecovery = resolvesIncident
    ? {
        incident_id: pending.recovery_incident_id,
        deployment_id: active.deployment_id,
        epoch: active.epoch,
        completed_at: nowIso(nowMs),
        outcome: "succeeded",
      }
    : state.last_recovery ?? null;
  let next = {
    ...state,
    active,
    pending: null,
    incident,
    last_recovery: lastRecovery,
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
  { enabled, intervalSeconds, failureThreshold },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  let next = {
    ...state,
    watchdog: {
      enabled,
      interval_seconds: intervalSeconds,
      failure_threshold: failureThreshold,
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
    if (
      incident &&
      incident.reason === "health_probe_failure" &&
      !["recovered", "resolved"].includes(incident.status)
    ) {
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
      reason: "health_probe_failure",
      status: "pending_dispatch",
      opened_at: probeAt,
      deployment_id: deploymentId,
      epoch: deploymentEpoch,
      failure_count: failures,
      last_error: String(detail || "health probe failed").slice(0, 1000),
    };
    shouldDispatch = true;
  } else if (
    incident &&
    incident.reason === "health_probe_failure" &&
    !["recovered", "resolved"].includes(incident.status)
  ) {
    incident = {
      ...incident,
      failure_count: failures,
      last_error: String(detail || "health probe failed").slice(0, 1000),
    };
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


export function evaluatePaidFallback(
  current,
  {
    leadSeconds = 1800,
  } = {},
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  const active = state.active;
  if (
    !active ||
    active.cost_class !== "paid" ||
    !active.paid_expires_at
  ) {
    return { state, shouldDispatch: false, expired: false };
  }

  const expiresMs = Date.parse(active.paid_expires_at);
  if (!Number.isFinite(expiresMs)) {
    throw new StateConflict("active paid fallback has an invalid expiry", 500);
  }
  const expired = nowMs >= expiresMs;
  const withinLead = nowMs + Math.max(0, leadSeconds) * 1000 >= expiresMs;
  if (!withinLead) {
    return { state, shouldDispatch: false, expired: false };
  }

  const existing = state.incident;
  if (
    existing &&
    !["recovered", "resolved", "failed"].includes(existing.status)
  ) {
    return {
      state,
      shouldDispatch:
        existing.reason === "paid_fallback_repatriation" &&
        existing.status === "pending_dispatch",
      expired,
    };
  }

  const incident = {
    id: crypto.randomUUID(),
    reason: "paid_fallback_repatriation",
    status: "pending_dispatch",
    opened_at: nowIso(nowMs),
    deployment_id: active.deployment_id,
    epoch: active.epoch,
    paid_expires_at: active.paid_expires_at,
    hourly_estimate_usd: active.hourly_estimate_usd ?? null,
    last_error: expired
      ? "paid fallback hard expiry reached"
      : "paid fallback is approaching hard expiry",
  };
  let next = { ...state, incident };
  next = event(next, "paid_fallback.repatriation_required", nowMs, {
    deployment_id: active.deployment_id,
    epoch: active.epoch,
    paid_expires_at: active.paid_expires_at,
    expired,
  });
  return { state: next, shouldDispatch: true, expired };
}

export function recordRecoveryOutcome(
  current,
  {
    incidentId,
    outcome,
    detail = "",
  },
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  if (!state.incident || state.incident.id !== incidentId) {
    throw new StateConflict("recovery incident is no longer current");
  }
  const allowed = new Set(["succeeded", "retryable", "blocked", "failed"]);
  if (!allowed.has(outcome)) {
    throw new StateConflict(`unsupported recovery outcome: ${outcome}`, 400);
  }

  const status = {
    succeeded: "resolved",
    retryable: "pending_dispatch",
    blocked: "blocked",
    failed: "failed",
  }[outcome];
  let next = {
    ...state,
    incident: {
      ...state.incident,
      status,
      last_outcome: outcome,
      last_outcome_at: nowIso(nowMs),
      outcome_detail: String(detail || "").slice(0, 1000),
      ...(outcome === "succeeded" ? { resolved_at: nowIso(nowMs) } : {}),
    },
  };
  next = event(next, `recovery.${outcome}`, nowMs, {
    incident_id: incidentId,
  });
  return next;
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

export function fenceResult(
  current,
  deploymentId,
  deploymentEpoch,
  nowMs = Date.now(),
) {
  const state = normalizeState(current);
  const active = state.active;
  const paidExpiryMs =
    active?.cost_class === "paid" && active?.paid_expires_at
      ? Date.parse(active.paid_expires_at)
      : null;
  const paidExpiryInvalid =
    active?.cost_class === "paid" &&
    (!active?.paid_expires_at || !Number.isFinite(paidExpiryMs));
  const paidExpired =
    active?.cost_class === "paid" &&
    (paidExpiryInvalid || nowMs >= paidExpiryMs);
  return {
    authorized:
      Boolean(active) &&
      !paidExpired &&
      active.deployment_id === deploymentId &&
      active.epoch === deploymentEpoch,
    active_epoch: active?.epoch ?? 0,
    leader_id: active?.deployment_id ?? "",
    reason: paidExpiryInvalid
      ? "paid_fallback_expiry_invalid"
      : paidExpired
        ? "paid_fallback_expired"
        : null,
  };
}
