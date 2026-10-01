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
| Systems → tools | `COMMAND_ACTION_SCOPES` exposes five operations | Native source/topic settings, voice, branding, review and publishing endpoints remain outside the agent |
| Async work → goal completion | A command launches workflows and returns; lifecycle callbacks record events | No generic goal scheduler resumes reasoning when search/analysis/render completes |
| Retrieval → reasoning | Fixed inspections and one binding round; source identity/edit recipe/recovery target still have heuristic selection | No adaptive read loop, query refinement, dependency graph, or general prerequisite repair |
| Tests → language reliability | Semantic tests mock planner outputs | Green CI proves contracts; it does not measure real paraphrase comprehension or provider availability |

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
