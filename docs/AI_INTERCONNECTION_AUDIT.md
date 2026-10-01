# AI interconnection audit — accepted main df17f16

## Finding

The LLM is still used as a constrained plan classifier and answer writer, rather
than a durable goal executor. PR #235 repaired semantic follow-ups and grounded
binding; it did not connect every native system or implement an agent loop.

## Evidence and consequences

| Connection | Evidence in code | Consequence |
| --- | --- | --- |
| Initial context → planning | `api/command_center.py` builds `planning_context` from history, selections and proposals before reading channel state | A new thread cannot reason from channel identity, configuration or production constraints |
| Language → clip retrieval | `services/command_center.py:best_clips` extracts literal words, defaults to today, fixes five results | Clear paraphrases can yield empty results; an unstated date restriction hides stored content |
| Channel → clip retrieval | `best_clips` searches scored clips globally; channel scoring does not establish ownership | A channel can receive candidates from another channel |
| Background result → next decision | Proposal context includes startup result; `command_activity` separately computes terminal state/events | Follow-ups can reason from stale “workflow started” evidence despite completed or failed work |
| Observation → binding authority | The second plan replaces the first without validating its action set or execution mode | An observation can introduce an operation or promote a preview to execution |
| Heavy planning → provider | `planner_provider_order` always prefers the conversation provider, including binding | Agent-provider preference applies to narration but not the consequential planning round |
| Retained knowledge → planning | Retained intelligence/trend evidence is available through typed attachments, not a general inspection | The model cannot independently consult this material before suggesting content |
| Systems → tools | `COMMAND_ACTION_SCOPES` exposes five operations | Native source/topic settings, voice, branding, review and publishing endpoints remain outside the agent |
| Async work → goal completion | A command launches workflows and returns; lifecycle callbacks record events | No generic goal scheduler resumes reasoning when search/analysis/render completes |
| Retrieval → reasoning | Fixed inspections and one binding round; source identity/edit recipe/recovery target still have heuristic selection | No adaptive read loop, query refinement, dependency graph, or general prerequisite repair |
| Tests → language reliability | Semantic tests mock planner outputs | Green CI proves contracts; it does not measure real paraphrase comprehension or provider availability |
| Transport → recovery | Each command creates a new server request UUID; proposal idempotency applies only after proposal creation | Resubmitting a lost direct-command response can create a second proposal/workflow |
| Latency → transport | Launcher API proxy uses a 120-second timeout; command rounds can each attempt several providers, Codex requests use 60 seconds | Provider timeouts are bounded individually, but there is no total command deadline or durable asynchronous command receipt |

## Required architecture

Use a typed capability catalog shared by planning and execution; scope-filter tools
per actor and channel. Supply current channel constraints and fresh observations.
Let the model select tool arguments and subsequent reads, with bounded steps/time/
budget and server validation. Persist a goal, ordered dependencies, authorization,
observations, and completion criteria. Resume through durable workflow events,
without requiring another operator message. Report waiting, blocked, failed and
completed distinctly. Existing publication approval and rights checks remain authoritative.

General knowledge may support labeled strategy and hypotheses; measured operational
claims require stored or retrieved evidence. Blocking invented facts must not prohibit
reasoning from evidence or cause the system to invent unavailable next steps.

## Scope of this pass

Repair verified context, retrieval, feedback, binding-authority and provider-routing
connections with regression coverage. Document rather than claim completion of
generic autonomous continuation, missing native tools, semantic source/recipe
resolution, and live model evaluation. No live provider results are fabricated.

## Repairs in this draft

- `command_environment` supplies current channel identity/timezone/local date,
  interests/exclusions, brand/ranking constraints, budget ceiling, and bounded
  source/recipe identities before the first plan. It reads rather than creates
  configuration; connection secrets/arbitrary profile metadata are omitted.
- The authenticated actor's action permissions are supplied to planning. The
  existing server permission checks remain authoritative before execution.
- Semantic clip lookup now accepts validated topic alternatives, time period and
  candidate count. With no requested date it searches all stored discovery dates;
  the older literal/today behavior remains only for the explicit offline route.
  Both routes now require channel ownership through the existing clip library
  lineage service. Retrieval still has a 250-row scored candidate pool limit.
- `research_context` exposes active retained intelligence and unexpired channel
  trend opportunities as a read capability. Model-interpreted optional topic terms
  filter saved data, with four research and four trend results per lookup.
  These records do not count as acquired media or live collection results.
- Fresh proposal activity, terminal state, terminal detail and resource status
  reach planning and narration on each subsequent turn. This reconnects feedback
  on operator turns; it does **not** wake an agent autonomously on completion.
- Observation binding uses the configured agent-provider preference. Paid fallback
  stays opt-in. A bound plan cannot add operations, promote proposals to execution,
  turn one-time work into recurring work, or add production preparation.
- Narration explicitly permits labeled inferences and reasoned strategy while
  retaining evidence requirements for operational facts.

## Remaining priorities

1. **Durable agent loop and transport receipt.** Persist an operator command identity
   and goal before inference; acknowledge it independently of a long HTTP request.
   Resume bounded reasoning from workflow outcomes, preserve authorization and
   idempotency across retries, and verify the goal's completion criteria.
2. **Shared typed capability catalog.** Readiness, permissions, argument/result
   schemas, prerequisites and handlers must come from the same catalog. Add native
   source/watch management, clip acquisition/review, editorial/voice/brand settings,
   recovery/cancellation and publication tools using their existing gates. Do not
   expose arbitrary APIs or infer publishing approval from unrelated preparation.
3. **Adaptive retrieval and semantic binding.** The model must be able to refine
   a query, choose another read, resolve a specific source/recipe/recovery target,
   and retrieve beyond the current bounded snapshots. Current source matching,
   recipe inference and first-failed-render choice still contain heuristics.
   Changing the selected model cannot supply missing capabilities.
4. **Live evaluations.** Evaluate actual configured providers on paraphrases,
   correction, negation, unclear identities, mixed goals, unavailable integrations,
   partial results, delayed completion, retries and goal completion. Score correct
   tool arguments and outcomes, not merely fluent answers. Tests must distinguish
   model evaluation from deterministic execution-contract checks.

## Validation

The regression suite includes real isolated SQLite saved-data retrieval and
lifecycle integration, plus the command handler with mocked model outputs. It
tests channel isolation, unstated dates, seven candidates, topic alternatives,
active retained research, current trends, terminal failure feedback, first-round
context/permissions, and prevention of observation-round authority expansion.
Full PostgreSQL CI and live provider evaluation are separate acceptance layers.
