import { DurableObject } from "cloudflare:workers";

import {
  StateConflict,
  abortPending,
  applyProbe,
  commitAuthority,
  configureWatchdog,
  fenceResult,
  markCandidateReady,
  markIncidentDispatchFailed,
  markIncidentDispatched,
  normalizeState,
  prepareAuthority,
} from "./state.mjs";

const STATE_KEY = "authority-state";

function json(data, status = 200) {
  return Response.json(data, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function bearerToken(request) {
  const header = request.headers.get("Authorization") || "";
  return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
}

function requireSecret(request, secret, name) {
  const expected = String(secret || "").trim();
  if (!expected) {
    throw new HttpError(503, `${name} is not configured`);
  }
  if (bearerToken(request) !== expected) {
    throw new HttpError(401, "unauthorized");
  }
}

function requireObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new HttpError(400, "request body must be a JSON object");
  }
  return value;
}

async function requestJson(request) {
  try {
    return requireObject(await request.json());
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, "request body must contain valid JSON");
  }
}

function deploymentId(value) {
  const text = String(value || "").trim();
  if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(text)) {
    throw new HttpError(400, "deployment_id is invalid");
  }
  return text;
}

function nonNegativeInt(value, name) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) {
    throw new HttpError(400, `${name} must be a non-negative integer`);
  }
  return number;
}

function positiveInt(value, name, maximum = Number.MAX_SAFE_INTEGER) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 1 || number > maximum) {
    throw new HttpError(400, `${name} must be an integer from 1 to ${maximum}`);
  }
  return number;
}

function httpsUrl(value, name) {
  let parsed;
  try {
    parsed = new URL(String(value || ""));
  } catch {
    throw new HttpError(400, `${name} must be a valid HTTPS URL`);
  }
  if (parsed.protocol !== "https:" || !parsed.hostname) {
    throw new HttpError(400, `${name} must be a valid HTTPS URL`);
  }
  return parsed.toString();
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function normalizeError(error) {
  if (error instanceof HttpError) return error;
  if (error instanceof StateConflict) {
    return new HttpError(error.status || 409, error.message);
  }
  return new HttpError(500, "internal coordinator error");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/healthz") {
      return json({ ok: true, service: "katcha-recovery-coordinator" });
    }
    if (!url.pathname.startsWith("/v1/")) {
      return json({ error: "not found" }, 404);
    }
    const objectName = String(env.AUTHORITY_OBJECT_NAME || "production");
    return env.AUTHORITY.getByName(objectName).fetch(request);
  },
};

export class RecoveryAuthority extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env;
  }

  async readState() {
    return normalizeState(await this.ctx.storage.get(STATE_KEY));
  }

  async updateState(mutator) {
    return this.ctx.storage.transaction(async (txn) => {
      const current = normalizeState(await txn.get(STATE_KEY));
      const result = mutator(current);
      const nextState = result?.state || result;
      await txn.put(STATE_KEY, nextState);
      return result;
    });
  }

  async fetch(request) {
    try {
      const url = new URL(request.url);
      const path = url.pathname;

      if (request.method === "POST" && path === "/v1/fence/assert") {
        requireSecret(request, this.env.FENCE_TOKEN, "FENCE_TOKEN");
        const body = await requestJson(request);
        const result = fenceResult(
          await this.readState(),
          deploymentId(body.deployment_id),
          positiveInt(body.deployment_epoch, "deployment_epoch"),
        );
        return json(result);
      }

      if (request.method === "GET" && path === "/v1/authority/status") {
        requireSecret(
          request,
          this.env.RECOVERY_ADMIN_TOKEN,
          "RECOVERY_ADMIN_TOKEN",
        );
        return json(await this.readState());
      }

      if (request.method === "POST" && path === "/v1/authority/prepare") {
        requireSecret(
          request,
          this.env.RECOVERY_ADMIN_TOKEN,
          "RECOVERY_ADMIN_TOKEN",
        );
        const body = await requestJson(request);
        const input = {
          deploymentId: deploymentId(body.deployment_id),
          healthUrl: httpsUrl(body.health_url, "health_url"),
          expectedActiveEpoch: nonNegativeInt(
            body.expected_active_epoch,
            "expected_active_epoch",
          ),
        };
        const result = await this.updateState((current) =>
          prepareAuthority(current, input),
        );
        return json({
          pending: result.pending,
          reused: result.reused,
          active_epoch: result.state.active?.epoch ?? 0,
        }, result.reused ? 200 : 201);
      }

      if (request.method === "POST" && path === "/v1/authority/candidate-ready") {
        requireSecret(
          request,
          this.env.RECOVERY_CANDIDATE_TOKEN,
          "RECOVERY_CANDIDATE_TOKEN",
        );
        const body = await requestJson(request);
        const result = await this.updateState((current) =>
          markCandidateReady(current, {
            deploymentId: deploymentId(body.deployment_id),
            deploymentEpoch: positiveInt(
              body.deployment_epoch,
              "deployment_epoch",
            ),
            readiness: requireObject(body.readiness),
          }),
        );
        return json({
          pending: result.pending,
          ready: true,
        });
      }

      if (request.method === "POST" && path === "/v1/authority/commit") {
        requireSecret(
          request,
          this.env.RECOVERY_ADMIN_TOKEN,
          "RECOVERY_ADMIN_TOKEN",
        );
        const body = await requestJson(request);
        const input = {
          deploymentId: deploymentId(body.deployment_id),
          deploymentEpoch: positiveInt(
            body.deployment_epoch,
            "deployment_epoch",
          ),
          expectedActiveEpoch: nonNegativeInt(
            body.expected_active_epoch,
            "expected_active_epoch",
          ),
        };
        const result = await this.updateState((current) =>
          commitAuthority(current, input),
        );
        await this.scheduleWatchdog(result.state);
        return json({
          active: result.active,
          active_epoch: result.active.epoch,
          leader_id: result.active.deployment_id,
        });
      }

      if (request.method === "POST" && path === "/v1/authority/abort") {
        requireSecret(
          request,
          this.env.RECOVERY_ADMIN_TOKEN,
          "RECOVERY_ADMIN_TOKEN",
        );
        const body = await requestJson(request);
        const input = {
          deploymentId: deploymentId(body.deployment_id),
          deploymentEpoch: positiveInt(
            body.deployment_epoch,
            "deployment_epoch",
          ),
        };
        const result = await this.updateState((current) =>
          abortPending(current, input),
        );
        return json({ aborted: result.aborted });
      }

      if (request.method === "POST" && path === "/v1/watchdog/configure") {
        requireSecret(
          request,
          this.env.RECOVERY_ADMIN_TOKEN,
          "RECOVERY_ADMIN_TOKEN",
        );
        const body = await requestJson(request);
        const enabled = body.enabled === true;
        if (enabled && !String(this.env.RECOVERY_DISPATCH_URL || "").trim()) {
          throw new HttpError(
            409,
            "RECOVERY_DISPATCH_URL must be configured before enabling watchdog recovery",
          );
        }
        const config = {
          enabled,
          intervalSeconds: positiveInt(
            body.interval_seconds ?? 60,
            "interval_seconds",
            3600,
          ),
          failureThreshold: positiveInt(
            body.failure_threshold ?? 3,
            "failure_threshold",
            20,
          ),
        };
        if (config.intervalSeconds < 30) {
          throw new HttpError(400, "interval_seconds must be at least 30");
        }
        const state = await this.updateState((current) =>
          configureWatchdog(current, config),
        );
        await this.scheduleWatchdog(state);
        return json({ watchdog: state.watchdog });
      }

      if (request.method === "POST" && path === "/v1/watchdog/probe-now") {
        requireSecret(
          request,
          this.env.RECOVERY_ADMIN_TOKEN,
          "RECOVERY_ADMIN_TOKEN",
        );
        return json(await this.runProbe());
      }

      return json({ error: "not found" }, 404);
    } catch (error) {
      const normalized = normalizeError(error);
      return json({ error: normalized.message }, normalized.status);
    }
  }

  async alarm() {
    try {
      await this.runProbe();
    } finally {
      await this.scheduleWatchdog(await this.readState());
    }
  }

  async scheduleWatchdog(state) {
    const current = normalizeState(state);
    if (!current.watchdog.enabled || !current.active) {
      await this.ctx.storage.deleteAlarm();
      return;
    }
    await this.ctx.storage.setAlarm(
      Date.now() + current.watchdog.interval_seconds * 1000,
    );
  }

  async runProbe() {
    const before = await this.readState();
    if (!before.watchdog.enabled || !before.active) {
      return {
        skipped: true,
        reason: "watchdog is disabled or no active deployment is committed",
      };
    }

    const observed = {
      deploymentId: before.active.deployment_id,
      deploymentEpoch: before.active.epoch,
    };
    let healthy = false;
    let detail = "";
    try {
      const response = await fetch(before.active.health_url, {
        method: "GET",
        headers: {
          Accept: "application/json",
          "Cache-Control": "no-cache",
          "User-Agent": "katcha-recovery-watchdog/1",
        },
        signal: AbortSignal.timeout(10_000),
      });
      healthy = response.ok;
      detail = healthy
        ? `HTTP ${response.status}`
        : `health endpoint returned HTTP ${response.status}`;
      if (response.body) {
        await response.body.cancel();
      }
    } catch (error) {
      detail = `${error?.name || "Error"}: ${error?.message || "health probe failed"}`;
    }

    const result = await this.updateState((current) =>
      applyProbe(current, { ...observed, healthy, detail }),
    );
    const incident = result.state.incident;

    if (
      !result.ignored &&
      incident?.status === "pending_dispatch" &&
      result.state.active?.deployment_id === observed.deploymentId &&
      result.state.active?.epoch === observed.deploymentEpoch
    ) {
      try {
        await this.dispatchRecovery(result.state, incident);
        await this.updateState((current) =>
          markIncidentDispatched(current, incident.id),
        );
      } catch (error) {
        await this.updateState((current) =>
          markIncidentDispatchFailed(
            current,
            incident.id,
            error?.message || String(error),
          ),
        );
      }
    }

    const latest = await this.readState();
    return {
      skipped: false,
      healthy,
      detail,
      active: latest.active,
      incident: latest.incident,
    };
  }

  async dispatchRecovery(state, incident) {
    const endpoint = httpsUrl(
      this.env.RECOVERY_DISPATCH_URL,
      "RECOVERY_DISPATCH_URL",
    );
    const token = String(this.env.RECOVERY_DISPATCH_TOKEN || "").trim();
    if (!token) {
      throw new Error("RECOVERY_DISPATCH_TOKEN is not configured");
    }
    const payload = {
      incident_id: incident.id,
      reason: "active_control_plane_health_threshold_exceeded",
      observed_at: new Date().toISOString(),
      active_deployment: state.active,
      expected_active_epoch: state.active?.epoch ?? 0,
      failure_count: incident.failure_count,
      last_error: incident.last_error,
    };
    const provider = String(
      this.env.RECOVERY_DISPATCH_PROVIDER || "generic",
    ).trim().toLowerCase();
    const headers = {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "Idempotency-Key": incident.id,
    };
    let body = payload;
    if (provider === "github") {
      headers.Accept = "application/vnd.github+json";
      headers["X-GitHub-Api-Version"] = "2026-03-10";
      body = {
        event_type: "katcha-recovery",
        client_payload: payload,
      };
    } else if (provider !== "generic") {
      throw new Error(`unsupported recovery dispatch provider: ${provider}`);
    }

    const response = await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) {
      const body = (await response.text()).slice(0, 500);
      throw new Error(
        `recovery dispatch failed with HTTP ${response.status}: ${body}`,
      );
    }
    if (response.body) {
      await response.body.cancel();
    }
  }
}
