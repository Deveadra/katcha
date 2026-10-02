# Runtime leadership fencing

Katcha's hosted control plane may have more than one compute instance over its lifetime. A stopped or reclaimed Oracle instance can later return after a replacement instance has already become authoritative. Process restarts, Temporal durability, and idempotent workflow IDs do not by themselves prevent that stale host from continuing an old activity.

Production therefore uses an **external deployment epoch fence**.

## Authority model

Every hosted deployment has:

- `deployment_id`: unique identity for that concrete control-plane deployment
- `deployment_epoch`: monotonically increasing positive integer
- a coordinator-managed active leader and epoch

The coordinator lives outside the OCI VM failure domain. The intended implementation is a SQLite-backed Cloudflare Durable Object.

Before an externally visible mutation, Katcha calls the coordinator. The mutation proceeds only when all three are true:

1. the coordinator returns `authorized: true`
2. `active_epoch` exactly equals the runtime's configured epoch
3. `leader_id` exactly equals the runtime's configured deployment ID

Coordinator failure is a **deny**, not an implicit grant.

Development and fixture runtimes keep fencing disabled. The hosted production validator rejects that setting.

## Assertion endpoint contract

Katcha sends:

```http
POST /v1/fence/assert
Authorization: Bearer <dedicated-fence-token>
Content-Type: application/json
```

Body:

```json
{
  "deployment_id": "oci-a1-20261002-001",
  "deployment_epoch": 12,
  "operation": "youtube.upload_chunk"
}
```

Successful active leader response:

```json
{
  "authorized": true,
  "active_epoch": 12,
  "leader_id": "oci-a1-20261002-001"
}
```

A stale or non-leader deployment should receive a normal authenticated response with `authorized: false` and the current authority fields. Authentication failures may return 401/403. Katcha fails closed on all transport failures, non-success status codes, malformed JSON, missing authority fields, stale epoch, or leader mismatch.

The fence token is independent from the user/operator control token.

## Current fenced mutation boundaries

The application asserts leadership before:

- starting a YouTube resumable upload
- every YouTube upload chunk
- YouTube snippet/status/thumbnail mutation
- R2/S3 bucket creation, writes, deletes, batch deletes, and moves
- render and thumbnail dispatch
- ChatGPT-plan inference
- Codex-plan inference
- direct OpenAI/Gemini command planning
- direct OpenAI/Gemini goal planning
- direct OpenAI/Gemini media analysis
- direct OpenAI/Gemini short-script generation
- direct OpenAI/Gemini long-form generation/critique
- OpenAI/Gemini/ElevenLabs TTS
- ElevenLabs preview synthesis

Read-only provider/status calls do not require leadership.

Future external compute adapters **must** call the fence before creating Fargate, Modal, Lambda, or other provider jobs.

## Failover sequence

A recovery coordinator must never make a replacement host authoritative merely because it booted.

Required order:

1. restore/attach durable state
2. start dependencies without permitting external mutations
3. run storage/database/Temporal reconciliation
4. choose a new monotonically increasing epoch
5. atomically publish the new `leader_id` + `active_epoch` in the Durable Object
6. install that identity into the replacement deployment
7. verify fence assertion
8. enable normal automation
9. leave every older deployment permanently fenced

When returning from paid fallback to Always Free, the A1 replacement gets another new epoch. Authority is never moved backward to an older epoch.

## Availability trade-off

The fence makes the recovery coordinator a small but intentional dependency for mutations. If the coordinator is unavailable:

- browsing/read-only state can continue where dependencies allow
- mutation-producing activities fail/retry instead of acting without authority
- no stale node is allowed to publish, spend provider credits, or overwrite production media

This is preferable to split-brain operation.

## Cutover gate

Before production cutover, acceptance must prove:

1. active deployment can mutate
2. same deployment with a stale epoch is denied
3. different deployment ID at the active epoch is denied
4. coordinator outage is denied
5. stale host cannot reach YouTube transport
6. stale host cannot write R2
7. stale host cannot dispatch render/provider work
8. after epoch handoff, the new leader succeeds without changing workflow identity
