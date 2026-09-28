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

Actions are separate from narration. The first contract supports:

- channel intelligence refresh;
- channel-scoped short production creation;
- channel-scoped compilation creation;
- dead-letter production render recovery.

`POST /v1/ai/actions/execute` requires `confirmed=true`. The browser deliberately uses a
two-click review/confirm interaction. Confirmed commands reuse Katcha's existing services and
Temporal workflows and emit `command_center.action_executed` into the domain event stream.

The initial compilation command intentionally uses Katcha's existing candidate-freeze policy.
Exact manual locking of an arbitrary set of selected clips is a separate control-plane
capability and should not be faked by the conversational layer.

## Initial natural-language intents

The first UI slice handles common operator questions directly:

- best/highest-scoring clips today;
- current production/render/publication failures;
- why a selected clip was rejected or scored the way it did;
- editing/performance evidence and behavior changes;
- content-creation requests, which become proposed actions rather than immediate execution;
- general channel status.

The intent layer is deliberately conservative. Unsupported or ambiguous language falls back to
a grounded channel-status answer instead of hallucinating a command.

## Auditability

The control API remains the source of truth. A Command Center answer contains its request ID,
intent, evidence records, proposed actions, and narrator identity. Execution creates the same
underlying production/workflow records as non-chat control calls and adds a high-level domain
event for external consumers such as Aerith.
