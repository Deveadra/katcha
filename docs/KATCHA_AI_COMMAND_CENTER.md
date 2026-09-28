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
- dead-letter production render recovery.

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

## Initial natural-language intents

The first UI slice handles common operator questions directly:

- best/highest-scoring clips in real channel-local windows such as today, yesterday or the last N hours/days;
- currently unresolved production/ranked-episode/render/publication failures, excluding older failed generations that were superseded by recovery;
- why a selected clip was rejected or scored the way it did;
- editing/performance evidence and behavior changes, including true channel-local "yesterday" windows;
- content-creation requests, which become proposed actions rather than immediate execution;
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

`*` preserves the existing single-operator setup. A later multi-principal credential layer can
assign different tokens/scopes to Aerith and human operators without changing the action API.
