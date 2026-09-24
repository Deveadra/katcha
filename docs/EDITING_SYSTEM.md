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
