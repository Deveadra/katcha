export class StateConflict extends Error {
  constructor(message, status = 409) {
    super(message);
    this.name = "StateConflict";
    this.status = status;
  }
}

const MAX_EXTERNAL_RESERVATIONS_PER_MONTH = 5000;
const MAX_EXTERNAL_RETRY_GROUPS = 10000;

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
    external_compute: {
      enabled: false,
      month_key: null,
      monthly_limit_microusd: 0,
      provider_limits_microusd: {},
      max_concurrent_jobs: 1,
      max_retry_attempts: 3,
      max_retry_spend_microusd: 0,
      monthly_settled_microusd: 0,
      provider_settled_microusd: {},
      retry_settled_microusd: {},
      retry_attempts: {},
      reservations: [],
    },
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
    external_compute: (() => {
      const raw = value.external_compute || {};
      const reservations = Array.isArray(raw.reservations)
        ? raw.reservations
        : [];
      const migratedMonthlySettled =
        raw.monthly_settled_microusd === undefined
          ? reservations
              .filter((row) => row?.status === "settled")
              .reduce(
                (total, row) =>
                  total + Number(row.actual_cost_microusd || 0),
                0,
              )
          : Number(raw.monthly_settled_microusd || 0);
      const migratedProviderSettled = {
        ...(raw.provider_settled_microusd || {}),
      };
      const migratedRetrySettled = {
        ...(raw.retry_settled_microusd || {}),
      };
      const migratedRetryAttempts = {
        ...(raw.retry_attempts || {}),
      };
      if (raw.provider_settled_microusd === undefined) {
        for (const row of reservations) {
          if (row?.status !== "settled") continue;
          const provider = String(row.provider || "");
          const value = Number(row.actual_cost_microusd || 0);
          migratedProviderSettled[provider] =
            (migratedProviderSettled[provider] || 0) + value;
        }
      }
      if (raw.retry_settled_microusd === undefined) {
        for (const row of reservations) {
          if (row?.status !== "settled") continue;
          const retryGroup = String(row.retry_group || "");
          const value = Number(row.actual_cost_microusd || 0);
          migratedRetrySettled[retryGroup] =
            (migratedRetrySettled[retryGroup] || 0) + value;
        }
      }
      if (raw.retry_attempts === undefined) {
        for (const row of reservations) {
          const retryGroup = String(row?.retry_group || "");
          if (!retryGroup) continue;
          migratedRetryAttempts[retryGroup] = Math.max(
            Number(migratedRetryAttempts[retryGroup] || 0),
            Number(row?.attempt || 0),
          );
        }
      }
      return {
        ...base.external_compute,
        ...raw,
        provider_limits_microusd: {
          ...base.external_compute.provider_limits_microusd,
          ...(raw.provider_limits_microusd || {}),
        },
        monthly_settled_microusd: migratedMonthlySettled,
        provider_settled_microusd: migratedProviderSettled,
        retry_settled_microusd: migratedRetrySettled,
        retry_attempts: migratedRetryAttempts,
        reservations,
      };
    })(),
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
  {
    enabled,
    intervalSeconds,
    failureThreshold,
    recoveryRetrySeconds = 900,
  },
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
    },
  };
  next = event(next, "watchdog.configured", nowMs, {
    enabled,
    interval_seconds: intervalSeconds,
    failure_threshold: failureThreshold,
    recovery_retry_seconds: recoveryRetrySeconds,
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


function monthKey(nowMs) {
  return new Date(nowMs).toISOString().slice(0, 7);
}

function normalizeExternalMonth(state, nowMs) {
  const current = normalizeState(state);
  const key = monthKey(nowMs);
  if (current.external_compute.month_key === key) {
    return current;
  }
  const carried = current.external_compute.reservations
    .filter(
      (row) => row.status === "reserved" || row.status === "expired",
    )
    .map((row) => ({ ...row, carried_into_month: key }));
  return {
    ...current,
    external_compute: {
      ...current.external_compute,
      month_key: key,
      monthly_settled_microusd: 0,
      provider_settled_microusd: {},
      reservations: carried,
    },
  };
}

function expireExternalReservations(state, nowMs) {
  const current = normalizeExternalMonth(state, nowMs);
  const timestamp = nowIso(nowMs);
  let changed = false;
  let monthlySettled = Number(
    current.external_compute.monthly_settled_microusd || 0,
  );
  const providerSettled = {
    ...(current.external_compute.provider_settled_microusd || {}),
  };
  const retrySettled = {
    ...(current.external_compute.retry_settled_microusd || {}),
  };
  const reservations = current.external_compute.reservations.map((row) => {
    if (
      row.status !== "reserved"
      || Number(row.expires_at_ms || 0) > nowMs
    ) {
      return row;
    }
    changed = true;
    if (row.settle_on_expiry === true) {
      const value = Number(row.estimated_cost_microusd || 0);
      const provider = String(row.provider || "");
      const retryGroup = String(row.retry_group || "");
      monthlySettled += value;
      providerSettled[provider] =
        Number(providerSettled[provider] || 0) + value;
      retrySettled[retryGroup] =
        Number(retrySettled[retryGroup] || 0) + value;
      return {
        ...row,
        status: "settled",
        actual_cost_microusd: value,
        settled_at: timestamp,
        metadata: {
          ...(row.metadata || {}),
          settlement_reason:
            "reservation expired; settled at reserved ceiling",
          auto_settled_on_expiry: true,
        },
      };
    }
    return {
      ...row,
      status: "expired",
      settled_at: timestamp,
    };
  });
  if (!changed) return current;
  return {
    ...current,
    external_compute: {
      ...current.external_compute,
      monthly_settled_microusd: monthlySettled,
      provider_settled_microusd: providerSettled,
      retry_settled_microusd: retrySettled,
      reservations,
    },
  };
}

function externalSpendSummary(state, nowMs) {
  const current = expireExternalReservations(state, nowMs);
  const external = current.external_compute;
  const settled = Number(external.monthly_settled_microusd || 0);
  let reserved = 0;
  const providerSettled = {
    ...(external.provider_settled_microusd || {}),
  };
  const providerReserved = {};
  const retrySettled = {
    ...(external.retry_settled_microusd || {}),
  };
  const retryReserved = {};
  let concurrent = 0;
  let expiredHeld = 0;

  for (const row of external.reservations) {
    const provider = String(row.provider || "");
    const retryGroup = String(row.retry_group || "");
    if (row.status === "reserved" || row.status === "expired") {
      const value = Number(row.estimated_cost_microusd || 0);
      reserved += value;
      if (row.status === "reserved") {
        concurrent += 1;
      } else {
        expiredHeld += value;
      }
      providerReserved[provider] = (providerReserved[provider] || 0) + value;
      retryReserved[retryGroup] = (retryReserved[retryGroup] || 0) + value;
    }
  }

  return {
    state: current,
    settled_microusd: settled,
    reserved_microusd: reserved,
    provider_settled_microusd: providerSettled,
    provider_reserved_microusd: providerReserved,
    retry_settled_microusd: retrySettled,
    retry_reserved_microusd: retryReserved,
    concurrent_jobs: concurrent,
    expired_held_microusd: expiredHeld,
  };
}

export function configureExternalCompute(
  current,
  {
    enabled,
    monthlyLimitMicrousd,
    providerLimitsMicrousd,
    maxConcurrentJobs,
    maxRetryAttempts,
    maxRetrySpendMicrousd,
  },
  nowMs = Date.now(),
) {
  let state = expireExternalReservations(current, nowMs);
  const limits = {};
  for (const [provider, raw] of Object.entries(providerLimitsMicrousd || {})) {
    const value = Number(raw);
    if (!provider || !Number.isSafeInteger(value) || value < 0) {
      throw new StateConflict("invalid external-compute provider limit", 422);
    }
    limits[provider] = value;
  }
  const retryAttempts =
    maxRetryAttempts ?? state.external_compute.max_retry_attempts ?? 3;
  if (
    !Number.isSafeInteger(monthlyLimitMicrousd) ||
    monthlyLimitMicrousd < 0 ||
    !Number.isSafeInteger(maxRetrySpendMicrousd) ||
    maxRetrySpendMicrousd < 0 ||
    !Number.isSafeInteger(maxConcurrentJobs) ||
    maxConcurrentJobs < 1 ||
    maxConcurrentJobs > 100 ||
    !Number.isSafeInteger(retryAttempts) ||
    retryAttempts < 1 ||
    retryAttempts > 100
  ) {
    throw new StateConflict("invalid external-compute budget configuration", 422);
  }
  state = {
    ...state,
    external_compute: {
      ...state.external_compute,
      enabled: enabled === true,
      monthly_limit_microusd: monthlyLimitMicrousd,
      provider_limits_microusd: limits,
      max_concurrent_jobs: maxConcurrentJobs,
      max_retry_attempts: retryAttempts,
      max_retry_spend_microusd: maxRetrySpendMicrousd,
    },
  };
  return event(state, "external_compute.configured", nowMs, {
    enabled: enabled === true,
    monthly_limit_microusd: monthlyLimitMicrousd,
    max_concurrent_jobs: maxConcurrentJobs,
    max_retry_attempts: retryAttempts,
    max_retry_spend_microusd: maxRetrySpendMicrousd,
  });
}

export function reserveExternalCompute(
  current,
  {
    jobKey,
    provider,
    operation,
    retryGroup,
    attempt,
    estimatedCostMicrousd,
    ttlSeconds,
    settleOnExpiry = false,
    metadata = {},
  },
  nowMs = Date.now(),
) {
  let state = expireExternalReservations(current, nowMs);
  const external = state.external_compute;
  if (!external.enabled) {
    throw new StateConflict("external compute is disabled", 423);
  }
  const existing = external.reservations.find((row) => row.job_key === jobKey);
  if (existing) {
    if (existing.status === "reserved" || existing.status === "settled") {
      return { state, reservation: existing, reused: true };
    }
    throw new StateConflict(
      `external-compute job key cannot be reused after ${existing.status}: ${jobKey}`,
    );
  }
  if (external.reservations.length >= MAX_EXTERNAL_RESERVATIONS_PER_MONTH) {
    throw new StateConflict(
      "external compute monthly reservation-count safety limit reached",
      429,
    );
  }
  const knownRetryGroups = new Set([
    ...Object.keys(external.retry_settled_microusd || {}),
    ...Object.keys(external.retry_attempts || {}),
    ...external.reservations.map((row) => String(row.retry_group || "")),
  ]);
  knownRetryGroups.delete("");
  if (
    !knownRetryGroups.has(retryGroup) &&
    knownRetryGroups.size >= MAX_EXTERNAL_RETRY_GROUPS
  ) {
    throw new StateConflict(
      "external compute retry-group state safety limit reached",
      429,
    );
  }
  const retryAttemptsUsed = Number(
    (external.retry_attempts || {})[retryGroup] || 0,
  );
  const resolvedAttempt =
    attempt === null || attempt === undefined
      ? retryAttemptsUsed + 1
      : Number(attempt);
  if (
    retryAttemptsUsed >= external.max_retry_attempts ||
    resolvedAttempt > external.max_retry_attempts ||
    resolvedAttempt <= retryAttemptsUsed
  ) {
    throw new StateConflict(
      `external compute retry attempt limit reached: ${retryGroup}`,
      429,
    );
  }
  if (!Object.prototype.hasOwnProperty.call(
    external.provider_limits_microusd,
    provider,
  )) {
    throw new StateConflict(
      `external compute provider has no configured ceiling: ${provider}`,
      422,
    );
  }
  if (
    !Number.isSafeInteger(estimatedCostMicrousd) ||
    estimatedCostMicrousd <= 0 ||
    !Number.isSafeInteger(ttlSeconds) ||
    ttlSeconds < 60 ||
    ttlSeconds > 604800 ||
    !Number.isSafeInteger(resolvedAttempt) ||
    resolvedAttempt < 1 ||
    resolvedAttempt > 100
  ) {
    throw new StateConflict("invalid external-compute reservation request", 422);
  }

  const summary = externalSpendSummary(state, nowMs);
  state = summary.state;
  if (summary.concurrent_jobs >= external.max_concurrent_jobs) {
    throw new StateConflict("external compute concurrency limit reached", 429);
  }

  const monthlyProjected =
    summary.settled_microusd +
    summary.reserved_microusd +
    estimatedCostMicrousd;
  if (monthlyProjected > external.monthly_limit_microusd) {
    throw new StateConflict("external compute monthly budget exceeded", 402);
  }

  const providerProjected =
    (summary.provider_settled_microusd[provider] || 0) +
    (summary.provider_reserved_microusd[provider] || 0) +
    estimatedCostMicrousd;
  if (
    providerProjected >
    Number(external.provider_limits_microusd[provider] || 0)
  ) {
    throw new StateConflict(
      `external compute provider budget exceeded: ${provider}`,
      402,
    );
  }

  const retryProjected =
    (summary.retry_settled_microusd[retryGroup] || 0) +
    (summary.retry_reserved_microusd[retryGroup] || 0) +
    estimatedCostMicrousd;
  if (retryProjected > external.max_retry_spend_microusd) {
    throw new StateConflict(
      `external compute retry budget exceeded: ${retryGroup}`,
      402,
    );
  }

  const reservation = {
    id: crypto.randomUUID(),
    job_key: jobKey,
    provider,
    operation,
    retry_group: retryGroup,
    attempt: resolvedAttempt,
    estimated_cost_microusd: estimatedCostMicrousd,
    actual_cost_microusd: null,
    status: "reserved",
    reserved_at: nowIso(nowMs),
    expires_at: nowIso(nowMs + ttlSeconds * 1000),
    expires_at_ms: nowMs + ttlSeconds * 1000,
    settled_at: null,
    settle_on_expiry: settleOnExpiry === true,
    metadata:
      metadata && typeof metadata === "object" && !Array.isArray(metadata)
        ? metadata
        : {},
  };
  state = {
    ...state,
    external_compute: {
      ...state.external_compute,
      retry_attempts: {
        ...(state.external_compute.retry_attempts || {}),
        [retryGroup]: resolvedAttempt,
      },
      reservations: [
        ...state.external_compute.reservations,
        reservation,
      ],
    },
  };
  state = event(state, "external_compute.reserved", nowMs, {
    reservation_id: reservation.id,
    job_key: jobKey,
    provider,
    retry_group: retryGroup,
    attempt: resolvedAttempt,
    estimated_cost_microusd: estimatedCostMicrousd,
  });
  return { state, reservation, reused: false };
}

export function settleExternalCompute(
  current,
  { reservationId, actualCostMicrousd, metadata = {} },
  nowMs = Date.now(),
) {
  let state = normalizeExternalMonth(current, nowMs);
  if (!Number.isSafeInteger(actualCostMicrousd) || actualCostMicrousd < 0) {
    throw new StateConflict("invalid external-compute settlement cost", 422);
  }
  const index = state.external_compute.reservations.findIndex(
    (row) => row.id === reservationId,
  );
  if (index < 0) {
    throw new StateConflict("external-compute reservation not found", 404);
  }
  const previous = state.external_compute.reservations[index];
  if (previous.status === "settled") {
    return { state, reservation: previous, reused: true };
  }
  if (!["reserved", "expired"].includes(previous.status)) {
    throw new StateConflict(
      `external-compute reservation cannot settle from ${previous.status}`,
    );
  }
  const reservation = {
    ...previous,
    status: "settled",
    actual_cost_microusd: actualCostMicrousd,
    settled_at: nowIso(nowMs),
    metadata: {
      ...(previous.metadata || {}),
      ...(metadata && typeof metadata === "object" && !Array.isArray(metadata)
        ? metadata
        : {}),
    },
  };
  const reservations = [...state.external_compute.reservations];
  reservations[index] = reservation;
  const provider = String(previous.provider || "");
  const retryGroup = String(previous.retry_group || "");
  state = {
    ...state,
    external_compute: {
      ...state.external_compute,
      monthly_settled_microusd:
        Number(state.external_compute.monthly_settled_microusd || 0) +
        actualCostMicrousd,
      provider_settled_microusd: {
        ...(state.external_compute.provider_settled_microusd || {}),
        [provider]:
          Number(
            (state.external_compute.provider_settled_microusd || {})[
              provider
            ] || 0,
          ) + actualCostMicrousd,
      },
      retry_settled_microusd: {
        ...(state.external_compute.retry_settled_microusd || {}),
        [retryGroup]:
          Number(
            (state.external_compute.retry_settled_microusd || {})[
              retryGroup
            ] || 0,
          ) + actualCostMicrousd,
      },
      reservations,
    },
  };
  state = event(state, "external_compute.settled", nowMs, {
    reservation_id: reservation.id,
    provider: reservation.provider,
    actual_cost_microusd: actualCostMicrousd,
  });
  return { state, reservation, reused: false };
}

export function releaseExternalCompute(
  current,
  { reservationId, reason = "" },
  nowMs = Date.now(),
) {
  let state = normalizeExternalMonth(current, nowMs);
  const index = state.external_compute.reservations.findIndex(
    (row) => row.id === reservationId,
  );
  if (index < 0) {
    throw new StateConflict("external-compute reservation not found", 404);
  }
  const previous = state.external_compute.reservations[index];
  if (previous.status === "released") {
    return { state, reservation: previous, reused: true };
  }
  if (!["reserved", "expired"].includes(previous.status)) {
    throw new StateConflict(
      `external-compute reservation cannot release from ${previous.status}`,
    );
  }
  const reservation = {
    ...previous,
    status: "released",
    settled_at: nowIso(nowMs),
    metadata: {
      ...(previous.metadata || {}),
      release_reason: String(reason || "").slice(0, 500),
    },
  };
  const reservations = [...state.external_compute.reservations];
  reservations[index] = reservation;
  state = {
    ...state,
    external_compute: {
      ...state.external_compute,
      reservations,
    },
  };
  state = event(state, "external_compute.released", nowMs, {
    reservation_id: reservation.id,
    provider: reservation.provider,
  });
  return { state, reservation, reused: false };
}

export function externalComputeStatus(current, nowMs = Date.now()) {
  const summary = externalSpendSummary(current, nowMs);
  const external = summary.state.external_compute;
  return {
    state: summary.state,
    status: {
      enabled: external.enabled,
      month_key: external.month_key,
      monthly_limit_microusd: external.monthly_limit_microusd,
      provider_limits_microusd: external.provider_limits_microusd,
      max_concurrent_jobs: external.max_concurrent_jobs,
      max_retry_attempts: external.max_retry_attempts,
      max_retry_spend_microusd: external.max_retry_spend_microusd,
      settled_microusd: summary.settled_microusd,
      reserved_microusd: summary.reserved_microusd,
      provider_settled_microusd: summary.provider_settled_microusd,
      provider_reserved_microusd: summary.provider_reserved_microusd,
      retry_attempts: {
        ...(external.retry_attempts || {}),
      },
      concurrent_jobs: summary.concurrent_jobs,
      expired_held_microusd: summary.expired_held_microusd,
      reservations: external.reservations.slice(-100),
    },
  };
}
