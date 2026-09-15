# Long-form compilation compiler

Katcha's long-form compiler turns the measured canonical clip library into review-ready 16:9 compilation videos without treating a compilation as a disguised Short production.

## Lifecycle

```text
queued
  -> selecting
  -> planning (Sol editor)
  -> critiquing (Gemini critic)
  -> optional Sol revision
  -> scripted
  -> voicing
  -> rendering
  -> review
  -> approved/rejected
  -> existing YouTube publication workflow
```

Every compilation is an immutable generation. Regeneration creates a child compilation rather than overwriting the rejected/failed parent.

## Candidate evidence

The first pass consumes no LLM tokens. Katcha scores clips using persisted evidence such as:

- the Phase 1 clip candidate score
- hook/payoff/surprise/rewatch features
- Short views and engaged views when available
- average viewed percentage
- share/comment/subscriber conversion signals
- canonical source duration and categories

Popularity is log-normalized so one very large Short cannot dominate every compilation. When measured YouTube data is not yet available, a scored canonical clip can still participate using its Phase 1 evidence.

The sequence optimizer then chooses a strong opener, reserves a strong closer, and penalizes adjacent category/tone/creator repetition. Theme matching is deterministic token overlap against the stored event/category/tone evidence; if the theme would leave too few usable clips, the compiler falls back to the full eligible scored pool rather than silently failing a potentially valid run.

The candidate snapshot is frozen on the compilation before paid editorial calls. Later analytics changes therefore cannot mutate an in-flight or historical compilation.

## Editor / critic loop

`LONGFORM_EDITOR` routes to GPT-5.6 Sol first and Gemini 3.8 Flash as fallback. `LONGFORM_CRITIC` uses the inverse primary/fallback order.

The editor receives the deterministic sequence plus evidence and creates a structured plan. The critic checks cold-open strength, repetitive adjacency, unnecessary narration, source-range validity, pacing, and audience fit. A `pass` verdict uses the plan directly; a `revise` verdict triggers one explicit Sol revision.

Each paid call is usage-ledgered against the compilation. Temporal automatic retry is disabled for paid editor, critic, revision, and TTS calls. If a provider may have accepted a request but Katcha could not safely persist the response, the generation fails visibly rather than silently spending again.

## Narration recovery

Narration is split into named roles: opening, optional intro, per-segment before/after lines, and optional outro. TTS writes the audio plus a JSON recovery sidecar to object storage before database reconciliation. If a worker fails after object persistence, a retry can reconcile that artifact without making another speech request.

## Rendering

Long-form output is reconstructed from each segment's canonical raw clip. Rendered Shorts are never used as source media.

The renderer receives a versioned `longform-render-v1` manifest. Vertical source video is shown as a contained foreground over a blurred, darkened copy of the same clip so the original composition is retained in a 16:9 frame. Narration interstitials use adjacent canonical media as a subdued moving background rather than generic filler graphics.

The manifest stores segment source ranges and output timing. That timing is written back to `compilation_segments`, which lets later retention analysis associate YouTube retention changes with the exact segment/host transition that was on screen.

## Review and regeneration

Human approval remains mandatory. Operators can approve, reject, or regenerate from:

- `plan`: rerun editorial planning (reusing the frozen candidate set when present)
- `voice`: reuse the final plan and create new narration
- `render`: reuse the final plan and narration, rebuilding only the render package

Each action leaves the original generation and its costs/history intact.

## YouTube publication

Approved compilations use the same Phase 3 `Publication` subsystem as Shorts. `publications` enforces exactly one source: either a Short production or a long-form compilation. Upload/session recovery, scheduling, processing checks, analytics, retention, and safe ambiguous-session retry behavior are therefore identical across both formats.

## Aerith boundary

Aerith is not imported by the long-form worker. Future Aerith control should use the public compilation/review/publication APIs and domain events, preserving Katcha as a separately deployable product.
