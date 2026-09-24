# Katcha Editing System

Status: P8 prototype foundation (#55)

Katcha editing is a channel-scoped production system, not an AI model being asked to "make a video." The reliable boundary is:

```
qualified content
  -> editorial intent
  -> frozen channel Edit Blueprint
  -> deterministic Render Plan
  -> stateless renderer
  -> QC / review gate
  -> scheduled publication
  -> outcome telemetry
```

## Why this boundary exists

AI is useful for semantic editorial decisions: what detail matters, what the host should say, what explanatory line belongs above a movie clip, and which approved treatment best fits the source. It should not invent arbitrary coordinates, shell commands, FFmpeg graphs, storage paths, timing primitives, or cross-channel styling.

Those mechanical choices are compiled from a finite, versioned Edit Blueprint. This gives Katcha reproducibility, channel isolation, bounded retries, understandable failures, and measurable creative treatments.

## Prototype blueprints

### `persona_commentary@1.0.0`

Designed for a channel such as RankSnaxx or another host-led property.

- full-frame vertical source
- blurred-fill background for non-vertical source
- required persona narration
- source audio ducks under narration
- narration captions enabled
- punch-cut treatment
- narration-density cap

### `header_explainer@1.0.0`

Designed for a clip-led channel where identity is the explanatory format rather than a host persona.

- persistent black header
- concise white explanatory text
- source video occupies the remaining frame
- no generated narration audio
- source audio retained
- no narration captions
- hard character cap on header copy

These are intentionally different editing grammars executed by the same renderer infrastructure.

## Render-plan contract

`blueprint-render-v1` freezes:

- channel profile ID
- brand key/version and brand render tokens
- blueprint key/version and complete blueprint snapshot
- media storage key and trim range
- derived source geometry
- header geometry/copy
- narration asset/timing/captions when permitted
- exact output duration, dimensions, FPS and storage key

The manifest validates its frozen blueprint and brand lineage before rendering. A header blueprint cannot silently become full-frame; a text-only blueprint cannot acquire TTS; required narration cannot disappear.

## Reliability rules

1. Paid AI/TTS decisions happen before deterministic compilation and are recorded separately.
2. Render-plan compilation is local, deterministic and safe to retry.
3. Renderer output keys are deterministic so retries can reuse an existing object.
4. A renderer executes the plan; it does not choose the channel identity.
5. Public upload/scheduling remains a separate automation gate.
6. Rights/acquisition eligibility remains upstream and cannot be weakened by editing policy.
7. Failed compilation or QC fails closed instead of silently substituting another channel template.

## Next production slices

The prototype deliberately avoids pretending the whole editor is complete. The production path should add:

- persisted multi-blueprint versions per channel;
- explicit blueprint selection frozen into ShortEpisode/Production records;
- pre-render QC for media existence, text fit, loudness, safe zones and duration;
- render revision/generation records with dead-letter recovery;
- durable workflow from approved render through scheduled publication;
- metrics keyed by blueprint/treatment so Katcha learns which editing grammar improves retention, engagement, return viewers and contribution margin.

The objective is not maximum content volume. It is reliable, channel-consistent throughput where creative changes are measurable and reversible.


## Persisted channel editing identity

Channel editing contracts are persisted independently from visual/persona branding. A channel may keep multiple blueprint families, but database constraints enforce one active version per family and one default blueprint per channel.

Every newly planned channel production or short episode freezes:

- `edit_blueprint_key`;
- the channel-local monotonic `edit_blueprint_version`;
- the complete `edit_blueprint_snapshot`.

Changing the active channel blueprint only affects future work. Regeneration copies the parent's frozen blueprint snapshot, so historical content cannot silently drift into a new editing treatment. An explicit future re-edit operation can intentionally select a newer version without weakening that lineage rule.

The control API exposes list, create, and activate operations under `/v1/channels/{channel_profile_id}/edit-blueprints`. Mutations serialize on the channel profile row and the database also owns partial unique indexes for active-family and default selection, preventing concurrent workers from producing two active identities.


## Executable channel routing

The production worker now treats the frozen Edit Blueprint as the editing dispatch boundary rather than assuming that every channel is a RankSnaxx-style host channel.

- `persona_commentary` keeps the mature multi-beat narration renderer used by host-led content. It may synthesize several commentary segments, duck source audio, show captions, and apply persona visuals.
- `header_explainer` uses the generic `BlueprintVideo` composition. It deliberately skips TTS, keeps the source audio, and turns the selected editorial angle into the persistent explanatory header required by the blueprint.
- Future text/source-only blueprint families can reuse `BlueprintVideo` without inheriting RankSnaxx copy, voice, ranking behavior, or visual identity.
- A production cannot silently switch blueprint or brand after creation: the frozen snapshots remain the source of truth for compilation and rendering.

This separation is important economically as well as creatively. A text-led clip channel does not pay for narration it does not use, while a personality-led channel can preserve the richer commentary treatment that gives it a recognizable host identity.

## Automated edit path

For a single-clip production the current automated path is:

```
qualified opportunity / clip
  -> frozen channel brand + edit blueprint
  -> AI semantic editorial intent
  -> conditional TTS (skipped for text/source-only blueprints)
  -> deterministic render-plan compilation
  -> pre-render asset + lineage validation
  -> Remotion render
  -> ffprobe duration/dimension verification
  -> object-store existence/non-empty verification
  -> review/automation gate
  -> existing idempotent YouTube publication workflow
```

The renderer is not allowed to decide identity. It receives an already compiled plan and either executes it exactly or fails.

## Failure behavior

Local compilation/render work remains bounded by the production Temporal workflow retry policy. A missing source object, missing narration asset, blueprint mismatch, invalid header, duration violation, failed media probe, dimension mismatch, or empty uploaded object fails closed rather than producing a publishable render.

A verified render carries verification metadata into the persisted render asset and emits a `production.render_verified` event. Publication still requires the existing approval, channel binding, credential, scheduling, and duplicate-upload gates.

The next hardening slice is durable render revision/dead-letter state plus an AutomationLevel-aware approved-render handoff that can construct the publication request from a channel-owned packaging/schedule policy. That handoff must reuse the existing publication ledger rather than introduce a second uploader.


## Durable render recovery and publish handoff

Rendering now has its own durable operational ledger. Every single-clip production or ranked short episode gets a `RenderAttempt` row before Remotion is called. Temporal retries reuse that same deterministic output and increment failure evidence on the same attempt. When bounded retries are exhausted, the attempt is marked `dead_letter`; it is not silently restarted.

Operator recovery creates a child content lineage from the render stage. Script selection, frozen brand, frozen Edit Blueprint and already-paid narration are reused when valid. Text/source-only blueprints explicitly do not require narration assets during recovery.

The channel AutomationLevel is evaluated only after a verified render exists:

```
review_required
  -> stop for human review

auto_approve_low_risk
  -> auto-approve only if every frozen acquisition snapshot is eligible + green lane
  -> stop before publishing

auto_publish_private
  -> same approval gate
  -> register one private Publication
  -> start the existing YouTube publication workflow

auto_publish_scheduled
  -> same approval gate
  -> choose the next collision-free learned/fallback channel window
  -> register one scheduled public Publication
  -> start the existing YouTube publication workflow
```

A manual approval on a higher automation level also resumes this handoff. This is intentional: content that required human review can continue automatically after that review rather than requiring a second manual publication action.

The handoff does not own an uploader. It calls the existing publication registration service, which keeps source/channel uniqueness constraints, channel binding, approval checks and duplicate-upload protection as the single publication ledger.

Automatic scheduling uses the latest channel `ScheduleRecommendation` set when available, otherwise the channel strategy fallback schedule. Blackout slots are excluded and already-occupied future publication times are skipped. If no safe slot, active YouTube credentials, verified render, or usable publication title exists, the handoff fails closed and leaves the approved media available for operator action.


## Edit blueprint performance attribution

The channel-intelligence cadence now closes the editing feedback loop. Publication registration freezes the shipped edit lineage into `Publication.treatment_metadata` for both single-clip productions and ranked short episodes. The record includes the channel blueprint revision, semantic blueprint contract version, composition, narration/layout modes, selected editorial style, voice, brand version and render-manifest version.

Each intelligence refresh creates an immutable `EditBlueprintPerformanceSnapshot`. It maturity-matches each publication to exactly one analytics snapshot near a standard outcome age (72 hours by default; 6/24/72/168-hour buckets are supported), so repeated YouTube samples do not turn one video into multiple observations and a week-old video is not casually compared with a six-hour-old upload. Groups are isolated by channel, source/format scope, blueprint revision and treatment; a ranked countdown is never compared directly with a single-clip explainer.

Tracked evidence includes:
- views and engaged views
- average view duration and percentage
- likes, comments, shares and subscriber movement
- audience-watch retention near 25%, 50%, 75% and 95% of runtime
- the existing normalized performance outcome score
- total attributed production cost across regeneration ancestry
- revenue coverage
- covered revenue, covered cost and covered contribution margin

Missing monetary analytics remain missing. Katcha calculates contribution margin only for the subset of publications with actual revenue data and reports monetary coverage beside it; an unknown revenue value is never converted into zero revenue.

Blueprint comparisons require at least five maturity-matched published samples in each group and only compare groups with the same channel + source/format scope. Retention deltas require at least 50% retention-data coverage. Margin deltas additionally require at least three revenue-covered publications in both groups and at least 60% monetary coverage. The resulting evidence is advisory only. P8.4 does not mutate the active blueprint automatically, which prevents a small or noisy sample from changing channel identity.

Control-plane endpoints:
- `GET /v1/channels/{channel_profile_id}/editing-performance`
- `GET /v1/channels/{channel_profile_id}/editing-performance/history`

The latest editing evidence is also embedded in the normal channel summary and is refreshed by the existing channel-intelligence schedule.
