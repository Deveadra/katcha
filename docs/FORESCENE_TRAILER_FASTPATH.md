# FORESCENE official-trailer fast path

This path is for time-sensitive, source-preserving video publications where Katcha
must not spend time generating narration or rendering an edit before upload.

It was introduced after the VisionQuest launch exposed three separate gaps:

1. retained external intelligence did not have an explicit bridge into discovery;
2. the generic production API did not expose the channel scope already supported by
   the production service;
3. packaging generation was post-publication oriented and did not version tags or
   hashtags.

The fast path closes those gaps without weakening acquisition or rights policy.

## What "passthrough" means

A passthrough production does **not** alter the acquired media. Katcha creates an
approved Production whose generation-1 `render` asset points at the already-ingested
source object.

It performs no:

- script generation;
- TTS/ElevenLabs call;
- Remotion render;
- InVideo handoff;
- re-encode.

The source SHA, source item, channel profile, brand lineage, acquisition state, and
provenance remain attached to the production.

Passthrough is an execution optimization, **not a rights shortcut**. A managed
discovery clip still has to satisfy Katcha's existing production-eligibility gate.
An official studio upload proves provenance; it does not by itself prove a reuse
license.

## End-to-end flow

### 1. Retain the discovery

Submit an intelligence batch through the handoff inbox or intelligence API.

For FORESCENE, every trailer record should include:

- the FORESCENE `channel_profile_id`;
- stable provider/video identity;
- official source URL;
- title and studio/channel;
- observed/release timestamp;
- provenance and confidence;
- urgency/trend context.

### 2. Materialize a discovery candidate

```http
POST /v1/channels/{channel_profile_id}/intelligence-records/{record_id}/discovery-candidate
```

This carries the intelligence record identity, title, summary, tags, payload,
provenance, and channel scope into the ordinary discovery/acquisition pipeline.

### 3. Qualify rights and acquire

Use the existing rights assessment/evidence endpoints. When the candidate is
production-eligible, promote it:

```http
POST /v1/discovery/candidates/{candidate_id}/promote
```

Promotion launches the existing ingest workflow. The downloader, SHA dedupe,
ffprobe validation, object-store persistence and SourceItem lineage remain unchanged.

### 4. Create a no-render production

```http
POST /v1/clips/{clip_id}/passthrough-productions
```

Example:

```json
{
  "channel_profile_id": "2a1dc777-fae4-4f5c-a6fd-eeabe4826af1",
  "idempotency_key": "visionquest-final-2026-10-01",
  "actor": "operator"
}
```

The production is immediately approved with a verified source-passthrough render
asset. The normal script/TTS/render workflow is never started.

The ordinary short-production endpoint also now accepts `channel_profile_id`;
channel branding must never be inferred by accidentally creating a shared production.

### 5. Create a publication on metadata hold

Create the publication with `hold_for_packaging=true`.

Katcha validates the approved source asset and YouTube channel scope but does not
start the upload workflow. The publication remains `queued / metadata_hold`.

This is the safe window for title/description/tag generation.

### 6. Generate SEO packaging candidates

```http
POST /v1/publications/{publication_id}/packaging/generations
```

Packaging candidate v2 carries:

- title;
- unique description;
- search intents;
- tags;
- hashtags;
- supporting grounded facts;
- thumbnail brief.

Generation is grounded in source lineage, channel metadata, recent retained channel
intelligence, and Katcha's own packaging-performance evidence when available.

Katcha should optimize for accurate viewer intent, not claim to "beat the
algorithm." YouTube's own guidance treats title/thumbnail/description and viewer
response as more important than tags. Tags are supplemental and especially useful
for entity variants and misspellings.

External tools such as VidIQ may later contribute keyword/research evidence through
the intelligence layer. They should not become the publication system of record.

### 7. Apply the chosen package before upload

```http
POST /v1/publications/{publication_id}/packaging/preupload
```

Example:

```json
{
  "variant_id": "<packaging-variant-id>",
  "actor": "operator"
}
```

The chosen immutable package becomes the publication title, description, tags and
hashtag block before any YouTube mutation occurs.

### 8. Release to YouTube

```http
POST /v1/publications/{publication_id}/start
```

The existing resumable YouTube workflow takes over:

1. upload private;
2. persist/resume byte offsets;
3. wait for YouTube processing;
4. finalize requested privacy/schedule state;
5. start analytics observations.

For urgent chronological trailer drops, acquire and prepare multiple videos in
parallel. Keep them on metadata hold/private processing as needed, then release the
public visibility in the desired order.

## SEO learning loop

Katcha does not know a universally "best" package in advance. The durable advantage
is the feedback loop:

- publication metadata and immutable variant lineage;
- impressions and CTR;
- views/watch percentage;
- midpoint retention;
- subscriber and revenue signals when available;
- channel-scoped chronological packaging evidence.

That evidence should improve future candidate generation and support controlled
package experiments. A third-party SEO product may supplement the evidence, but
Katcha owns the final package, mutation history and performance attribution.
