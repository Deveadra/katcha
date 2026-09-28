# Katcha AI Command Center

Katcha has two control surfaces with intentionally different callers.

```text
Human operator
    |
    v
Katcha AI Command Center
    |
    v
Katcha authenticated control API
    |
    v
Katcha control plane
    |---- AI routing / budgets
    |---- Temporal workflows
    |---- renderers
    |---- review gates
    |---- publishing

Aerith / Ultron
    |
    +---- Katcha authenticated control API
    +---- /v1/control/events + acknowledgements
                 |
                 v
          Katcha control plane
```

## Boundary

The Command Center is a **human-facing interpretation layer**. It may translate natural
language into a grounded read of Katcha state and may propose a structured action, but the
language model is never authoritative for Katcha state and never receives implicit permission
to mutate it.

Aerith/Ultron should **not** automate Katcha by sending chat prompts to the Command Center.
External orchestration uses the authenticated control API and cursor-based event stream
directly.

## Grounding

`POST /v1/ai/command` scopes every request to one channel profile. Katcha first gathers
structured evidence from its own database/services. In live AI mode, the existing
channel-aware `performance_analysis` route may turn that evidence into a more natural
explanation. If AI execution or a provider is unavailable, the endpoint still returns the
deterministic grounded summary.

Provider narration is instructed not to invent missing metrics, failures, causes, clips, or
executed actions. Evidence is returned alongside the answer so the operator can inspect what
the narrative was based on.

## Actions

Actions are separate from narration. Katcha persists every executable suggestion as a
server-issued `CommandActionProposal`. A proposal freezes the channel, action type, payload,
expiry and deterministic idempotency key before the browser sees it. The browser never sends
an executable payload back to Katcha.

The current contract supports:

- channel intelligence refresh;
- channel-scoped short production creation;
- exact selected-clip ranked episode creation;
- dead-letter production render recovery;
- channel-scoped autonomous source scouting.

Execution is `POST /v1/ai/actions/{proposal_id}/execute` with only `confirmed=true`.
The server reloads the proposal, derives the actor from authenticated control-plane state,
checks the required scope, atomically claims the proposal and executes only the frozen
server-side payload. Duplicate confirmation returns the same proposal/result instead of
creating a second production. A stale executing claim can be safely retried because Temporal
workflow IDs and production/episode idempotency are derived from the proposal.

For a ranked short episode, selected clip membership **and selection order** are frozen.
The active channel brand, editorial format and edit blueprint are frozen by the existing short
episode service. Plain-language "commentary recipe" resolves to the channel's
`persona_commentary` blueprint family; the exact stored blueprint version is recorded on the
episode.

Proposal lifecycle events (`proposal_created`, `action_confirmed`,
`action_retry_claimed`, `action_executed`, `action_failed`, `proposal_expired`) flow
through the existing domain-event stream for Aerith and other consumers.

A source-scout proposal freezes the requested platforms, topical terms, cadence and
operator request server-side before confirmation. Execution creates or reuses a
channel-scoped `TopicWatchVersion` backed by `web_scout@v1` and starts its
durable Temporal schedule. The default cadence is hourly with a 24-search-per-day
provider cap. Each cycle carries bounded exploration memory so it can branch toward
adjacent creators, communities, sites and newer posts rather than repeatedly
returning the same pages. Discovery does not authorize rendering or publishing.

## Initial natural-language intents

The first UI slice handles common operator questions directly:

- best/highest-scoring clips in real channel-local windows such as today, yesterday or the last N hours/days;
- currently unresolved production/ranked-episode/render/publication failures, excluding older failed generations that were superseded by recovery;
- why a selected clip was rejected or scored the way it did;
- editing/performance evidence and behavior changes, including true channel-local "yesterday" windows;
- content-creation requests, which become proposed actions rather than immediate execution;
- source-discovery requests such as "Find new TikTok, Instagram, X, and Bluesky sources";
- general channel status.

The intent layer is deliberately conservative. Unsupported or ambiguous language falls back to
a grounded channel-status answer instead of hallucinating a command.

## Auditability

The control API remains the source of truth. A Command Center answer contains its request ID,
intent, evidence records, proposed actions, and narrator identity. Execution creates the same
underlying production/workflow records as non-chat control calls and adds a high-level domain
event for external consumers such as Aerith.


## Control identity and scopes

The current control plane still uses one configured bearer token, but Katcha no longer trusts a
client-supplied `actor` for Command Center mutations. Authentication derives a non-secret token
fingerprint for the audit actor and binds scopes from `KATCHA_CONTROL_API_SCOPES`.

Command Center scope checks currently use:

- `ai:read`
- `intelligence:write`
- `production:create`
- `render:recover`
- `discovery:write`

`*` preserves the existing single-operator setup. A later multi-principal credential layer can
assign different tokens/scopes to Aerith and human operators without changing the action API.


## Durable conversation history

Command Center conversations are now server-side records rather than browser-only state.

- `command_threads` stores the channel scope, title, server-derived creator, status and activity timestamps.
- `command_turns` stores ordered user/assistant turns, request IDs, narrator identity, grounded evidence and typed command context.
- `POST /v1/ai/command` accepts an optional `thread_id`. New commands create a thread automatically; follow-up commands append to the same durable thread.
- `GET /v1/ai/threads` lists active conversations for one channel.
- `GET /v1/ai/threads/{thread_id}` reconstructs the turns plus the proposals attached to those turns.
- `POST /v1/ai/threads/{thread_id}/archive` hides a completed conversation without deleting the audit record.

The browser's **New conversation** action no longer destroys history. Existing conversations can be reopened from the channel-scoped history selector.

A user/assistant exchange is recorded atomically under a row lock so the thread cannot persist only half of a successful answer.

## Correlation for external orchestration

Every server-issued action proposal can reference both the conversation thread and the exact assistant turn that proposed it. Proposal lifecycle events carry:

- `request_id`
- `thread_id`
- `source_turn_id`
- `proposal_id`
- `channel_profile_id`
- action type and actor

Successful execution emits both `command_center.action_executed` and `command_center.workflow_started` when an underlying workflow ID exists. The result contains the authoritative production/episode/workflow identifiers that Aerith can use to correlate later resource lifecycle events.

Render lifecycle events now include `channel_profile_id`, and production creation/regeneration events expose their workflow IDs. This keeps render failures and completions visible to channel-scoped consumers of `/v1/control/events`.


## Grounded conversational follow-ups

Durable threads now participate in command resolution rather than acting as transcript storage only.

Katcha can deterministically resolve common references against the latest grounded turn:

- `Why?` after a ranked clip answer resolves to the first applicable clip.
- `Turn the second one into a short` resolves the ordinal against the prior clip evidence.
- `Make those into an episode` resolves the ordered prior clip set without re-ranking it.
- `What about yesterday?` can inherit the prior best-clips question type and topic terms while applying the new time window.
- Explicit clip selections always remain authoritative over inferred conversational references.

The API returns a typed `resolved_context` record with the resolved clip IDs, source turn and resolution reason. The web UI reflects inherited selections visibly before any proposal can be confirmed.

Conversational acknowledgements such as `yes, do it`, `go ahead`, or `confirm` never execute a control-plane action. When the immediately preceding grounded answer has a pending proposal, Katcha re-presents that same frozen server-issued proposal for explicit review and confirmation instead. This remains true even when the UI currently has clip context selected.


## Action and workflow activity

Confirmed actions no longer stop at a workflow ID in the chat UI.

`GET /v1/ai/actions/{proposal_id}/activity` resolves the authoritative result resource created by the proposal and reports its current Katcha state. For production and ranked-episode actions this includes the resource ID, workflow ID, generation, status, stage, error, update time, and a compact event timeline. Proposal lifecycle events and resource lifecycle events are correlated through the proposal result IDs.

The Command Center checks activity immediately after an action is accepted and polls while the resource is still moving. It stops automatically when the resource reaches a human-review or settled state such as `review`, `approved`, `rejected`, or `failed`. Reopened executed actions also expose an explicit workflow-status check.

This distinguishes two separate facts in the UI:

- **Action executed** means Katcha accepted the confirmed command and started or registered the authoritative workflow/resource.
- **Workflow/resource state** shows what happened after that handoff, including whether the media is still processing, is awaiting review, or failed.

The distinction prevents an accepted command from being mistaken for a completed render or publication.


## Constrained command planning

Katcha AI no longer has to treat every unfamiliar phrasing as a generic channel-status
request. A small **read-only command planner** handles only requests that the deterministic
router cannot classify confidently.

The planner is schema-constrained to this registry:

- `best_clips`
- `failures`
- `clip_rejection`
- `clip_explanation`
- `performance_advice`
- `create_content`
- `source_discovery`
- `channel_status`
- `unsupported`

The model cannot name arbitrary tools, execute an action, confirm an existing proposal, or
write directly to Katcha state. Clear deterministic commands bypass the planner entirely.
Ambiguous requests use the low-cost `command_planning` AI route. A plan below the confidence
threshold falls back to channel status, and provider/routing failures also fail closed.

Planning metadata is returned with the Command Center response and stored with the assistant
turn: resolved intent, deterministic/AI source, provider/model, confidence and reason. The
Command Center evidence panel exposes this as **Command Routing**, making it possible to
inspect why Katcha interpreted a request the way it did.

The planner does not weaken the mutation boundary. A `create_content` or
`source_discovery` plan can only produce the same frozen server-issued proposal used
elsewhere; execution still requires the separate authenticated confirmation endpoint.
