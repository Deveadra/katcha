# Katcha semantic planning and follow-through audit

The product contract is a goal-directed assistant using native Katcha systems.
Operators must not learn command phrases or internal operation names. Implemented
tools and typed arguments are necessary; an utterance dictionary is not the agent.

## Root causes found

- The planner selected one intent without receiving actual conversation turns,
  grounded resource identities, or frozen pending actions.
- Live follow-through could bypass planning through a confirmation phrase matcher.
  Broad prefix matching could also misread negation.
- Low-confidence model results could be replaced by a literal keyword route.
- A single intent could not inspect several systems before deciding what to do.
- Action arguments were often compiled before the model saw returned clip records.
- Every new action required another confirmation turn, even for a direct instruction.
- Response generation preceded execution, so narrative/history could describe a
  prepared proposal instead of the observed result.
- Generic source scouting defaulted to continuous scheduling, even for a one-time search.
- Synchronous inference ran inside the async HTTP handler; Gemini calls had no
  explicit request timeout.
- Workflow result annotations used `dict[str, object]`. The installed Temporal
  converter rejects heterogeneous values with this hint, so a valid child result
  could fail before the parent could continue. Workflow JSON result hints now use
  `Any`, verified against the real SDK converter across registered workflow classes.

## Implemented control flow

1. Supply bounded real history, selected resources, and frozen action records to
   the model. Preserve the operator's original wording in live mode.
2. Interpret the goal, read capabilities, requested operations, execution intent,
   source/query/media constraints, and grounded references semantically.
3. Inspect up to six capabilities. Keep other observations when a secondary read
   fails and record the failure as evidence.
4. For requested new actions, run one additional observation/binding round using
   real returned records. Validate clip references against those records.
5. Compile native operation arguments on the server. Persist the plan, conversation
   turn, and frozen proposals before execution. No invented action name or ID is accepted.
6. Direct instructions may run registered operations; suggestions remain proposals.
   Existing proposals can be selected by meaning, including multiple or older ones.
   Preflight `ai:write` and every operation scope before starting any selected action.
7. Claim idempotently, execute, record outcomes, then generate and persist the final
   answer from actual action evidence. Workflow startup is not downstream completion.
8. Keep offline routing explicitly degraded; it has no live semantic execution authority.

Live inference uses worker threads, and Gemini HTTP requests have a 30-second
request timeout with no hidden SDK retries before provider fallback. Each planning
round has a distinct budget reservation identity.
One-time web scouting uses a bounded discovery workflow; recurring scouting requires
explicit semantic intent. Existing legacy recurring proposal payloads remain supported.

## Use-case coverage and remaining work

| Use case family | Current command capability | Important boundary / remaining work |
| --- | --- | --- |
| General questions and strategy | Context-aware narration and stored channel state | No measured claims without evidence; live comprehension needs model evaluation |
| Clip discovery and comparisons | Stored clip inspection plus external source scouting | Fresh discovery returns asynchronously; generic re-planning after a background search is not implemented |
| Named official source search | Resolve enabled channel/shared source, freeze channel reference, search once | Unknown official account resolution and saving a newly discovered source are separate missing tools |
| Trailer/teaser preparation | Typed media constraints, matching promotion, ingest and analysis workflow | Existing rights/originality gates remain; reused clip analysis readiness needs further runtime verification |
| Performance and failures together | Multiple read capabilities in one request | Partial secondary-read failure is visible; no cross-channel aggregation tool yet |
| Short or ranked production from observed clips | Observation round resolves actual clips, native production arguments | Supported channel formats/counts and brand/editorial gates still apply |
| Recovery and intelligence refresh | Native recovery / refresh operations | Only registered recovery types are available; cannot promise arbitrary repairs |
| Direct operator instructions | Persist, validate permissions, run native work | No publishing, deletion, or arbitrary settings mutation through this registry |
| Follow-up authorization | Model-selected exact frozen proposal IDs | Changed arguments require a new proposal; expired proposals cannot be executed |
| Several pending actions | Specific selection or explicit multiple IDs, preflight all scopes | Ambiguous selections ask a targeted question; no guessing by list position in live routing |
| Negation, hypotheticals, explanation first | Model can choose discussion/inspection with no requested actions | Mocked tests verify the execution contract, not live model reliability |
| Retries and already-started work | Existing proposal claim/idempotency and result records | An uncertain HTTP result must not be reported as completed production |
| Missing data or configuration | Explicit capability-gap evidence, distinct from unclear language | Further prerequisite-specific repair tools are needed |
| Provider outage / low confidence | Degraded saved-data reads or specific clarification; no keyword-based live execution | Provider credentials, quota, and actual live inference require deployed verification |
| Source configuration and topic-watch management | Native APIs exist | Save/update/disable tools are not yet exposed to Command Center |
| Voice, branding, editing and retention settings | Native APIs exist | Typed, scoped command tools and their validation are still required |
| Review, approval, publishing and analytics sync | Native APIs exist | Not exposed as generic command actions; publishing must retain its explicit product approval contract |
| Cancel/modify background work and durable multi-stage goals | Workflow/action records exist | Generic cancellation, dependency graph execution, background result-driven re-planning, and goal completion monitoring remain open |

This is a bounded semantic planner with native execution, not a claim that every
native API is already an agent tool. To expand coverage, add a documented typed
capability, server handler, permissions, observation/result schema, and lifecycle
checks. Do not add special utterances or canned replies for each new use case.

## Validation

`tests/test_semantic_command_planning.py` runs the real command handler with mocked
planning, persistence, and workflow execution. It checks grounded IDs, cross-channel
rejection, varied follow-ups, negative language bypassing the old phrase gate,
idempotence, permission preflight, direct instruction persistence order, multi-system
inspection, partial read failures, specific clarification, and observed multi-clip
production binding. These are deterministic contract tests, not live model evaluations.

Existing planner, narrator, YouTube adapter, source workflow, and consolidated worker
registration tests are also exercised. PostgreSQL history-finalization regression
coverage runs in full CI. Full CI, deployed credentials, live provider semantics, and
actual background discovery/ingest still require verification before production readiness.
