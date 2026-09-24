# Trend explorer and AI controller contract

The first-party explorer is served by FastAPI at `/explorer`. Its assets ship in the
Python package and Docker image. The GUI and AI controllers use the same `/v1` APIs.
The separate ChatGPT Sites prototype remains a simulated design preview; it does not
connect to this backend and is not the production deployment.

## Private operation

Run the existing database migrations and Katcha API/workers, then open
`http://localhost:8000/explorer`. A live database and configured channel are required.
The UI never falls back to fictional data on missing results or errors.

For a hosted deployment set `KATCHA_ENV=production` and a strong random
`KATCHA_CONTROL_API_TOKEN` through the deployment's secret manager. Serve behind HTTPS
and owner-only network/access controls. Enter the token in the explorer's Connect form;
it remains in tab memory, is cleared from the input, and is not written to local storage.
Machine clients send `Authorization: Bearer <token>` on every request. This is a
single-operator credential with access to all configured channels, not multi-user RBAC.
Channel-scoped handlers validate record ownership; possession of a channel UUID is not
a substitute for authentication.

When a token is configured it protects all control-plane endpoints, including existing
mutation routes. Health probes and the YouTube OAuth callback are exempt; the latter
retains the existing signed/stateful OAuth validation. Production refuses control
requests with 503 if no token is configured. Local development without a token must
remain on loopback or a trusted private network. Static assets/API documentation contain
no channel data and can load before login. Never place the control token or provider
credentials in frontend files, query strings, or source control.

The default Docker API image does not install optional AI SDKs. For AI workers use the
existing production image/Compose packaging with AI dependencies and configure provider
keys, channel budgets, storage, renderer, and Temporal before enabling paid execution.
No paid model calls or real publications are run by the explorer test suite.

## Shared read API

| Endpoint (GET) | Result |
|---|---|
| `/v1/channels/{channel}/trends/explorer` | Latest unexpired snapshot per topic for the current channel watch; optional limit 1–100. |
| `/v1/channels/{channel}/trends/explorer/{opportunity}` | Versioned dossier: stored score/components, packet, and raw signal series; hours 1–720, limit 1–1000. |
| `/v1/channels/{channel}/trends/explorer/{opportunity}/episodes` | Up to 50 linked episodes and their frozen-evidence readiness. |

Board ordering uses the calibrated score when present, otherwise the deterministic
score. It never recalculates opportunity scores in JavaScript. After changing interests,
refresh intelligence to create snapshots for the new watch version. A refresh scores
stored observations; discovery watches execute/schedule provider collection separately.

The dossier uses the opportunity's watch version and creation time. Signal queries apply
platform/language/region filters before the limit and exclude observations after the
snapshot. `truncated` means only the newest bounded observations were returned. Charts
show one source entity and one cumulative metric at a time; they never sum Reddit
upvotes with YouTube views or fabricate zero values for missing data. Evidence packets
can be absent when a topic is not qualified; that state is visible.

## AI editorial integration

1. A controller reads opportunities/dossiers and chooses a qualified, current opportunity.
2. Existing discovery/acquisition/analysis APIs obtain eligible analyzed clips. Source
   URLs in a packet are evidence pointers, not automatic acquisition authorization.
3. The controller calls `POST /v1/short-episodes` with the existing request contract:
   `channel_profile_id`, `trend_opportunity_id`, `premise`, scored `candidates`, an explicit
   `idempotency_key`, and optional format/item count. Candidate scores must come from
   real analysis/editorial evaluation; the explorer does not invent them.
4. Registration checks current watch version, expiry, deterministic score/confidence,
   evidence presence, channel ownership, and the existing clip eligibility gates. It
   freezes a source context capped at 12,000 characters into `plan_snapshot.trend_context` in the same
   transaction as the episode plan. Packet ID, version and hash remain traceable.
5. Either the GUI or the controller calls
   `POST /v1/channels/{channel}/trends/explorer/{opportunity}/editorial` with
   `{"episode_id": "<planned episode UUID>"}`. This validates channel/opportunity linkage
   and frozen evidence, then invokes the existing AI/provider-gated Temporal script
   workflow. The workflow ID is stable and duplicate reuse is rejected, including after
   completion. Recovery uses the existing regeneration lineage rather than a new paid
   retry of the same episode.
6. The AI script prompt receives both clip analysis and the frozen trend context. Source
   claims are explicitly unverified, external text is untrusted data rather than commands,
   and rights are not inferred. The existing budget reservation, routing, structured
   generation, validation, voice, review, rendering, publication and analytics paths remain
   authoritative. These sources assist framing; they do not replace factual verification.

An already-planned episode keeps its frozen evidence even if later signals change. Old
plans without a frozen packet are visible but cannot use the new handoff; create a new
qualified plan with a new idempotency key. Standard legacy editorial routes remain
compatible with episodes that were not created from trends.

This integration makes the GUI optional for controllers and fixes the evidence-to-AI
prompt gap. It does not implement a new unattended scheduler that chooses and acquires
clips automatically, nor does it enable automatic publishing. Remaining provider quota,
observability and autonomous acquisition work remains tracked in #13, #18, #43 and #46.

## Validation

`python -m pytest -q` covers SQL snapshot selection, time bounds and source filters,
channel isolation, authentication, evidence qualification/bounds, prompt grounding, and
workflow handoff. `cd web-tests && npm ci && npx playwright install chromium && npm test`
exercises UI behavior against explicitly labeled HTTP fixtures in Chromium, including
mobile overflow, watch setting preservation and exact handoff requests. CI retains
screenshots as artifacts. Fixture data is never bundled with the shipped UI.
