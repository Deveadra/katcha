# Interim intelligence ingestion

Katcha can retain operator- or assistant-generated intelligence while autonomous
discovery is still under development. The ingestion contract is deliberately
channel-scoped so research for one channel cannot silently become context for
another.

## What belongs here

Use intelligence records for durable knowledge that is broader than a raw
discovery URL:

- `source`: a site, feed, community, creator source, or other place worth watching.
- `channel`: a YouTube/Twitch/social channel and observations about its strategy.
- `clip` or `video`: an externally observed piece of content worth retaining.
- `topic` or `trend`: a story, game, release, meme, or emerging opportunity.
- `editorial_decision`: a decision to pursue, reject, defer, or reshape an idea.
- `script`: a script snapshot or rationale that should survive chat history.
- `packaging`: title, thumbnail, metadata, or positioning intelligence.
- `performance`: observed performance data or a lesson derived from it.
- `note`: durable context that does not fit a more specific kind.

Kinds are extensible slugs rather than a database enum so Katcha can add future
record types without a migration.

Raw URL discovery still belongs in the existing ingestion-source/discovery
pipeline. This layer complements that pipeline instead of replacing it.

## Stable identity

Every record has two identity fields:

- `record_kind`
- `record_key`

The pair is unique within a channel. Re-ingesting the same pair from a later
batch updates the durable record while preserving its first and latest batch
references.

Prefer provider identities when they exist:

- `youtube:channel:UC...`
- `youtube:video:dQw4w9WgXcQ`
- `reddit:subreddit:gaming`
- `topic:grand-theft-auto-vi-release-window`

For records without a provider ID, generate a deterministic key from the subject
rather than a random UUID.

## Batch idempotency

POST batches to:

`POST /v1/intelligence-ingest/batches`

A batch is uniquely identified by `channel_profile_id + batch_key`.

Katcha hashes the normalized batch contents. Replaying the same batch returns the
same stored batch and records. Reusing the same key with different content is a
conflict instead of silently mutating history.

Example:

```json
{
  "channel_profile_id": "00000000-0000-0000-0000-000000000000",
  "batch_key": "orion-2026-09-30-opportunity-scan-001",
  "producer": "orion",
  "source_type": "assistant",
  "batch_metadata": {
    "purpose": "interim_rank_snaxx_operations"
  },
  "records": [
    {
      "record_kind": "channel",
      "record_key": "youtube:channel:UC-example",
      "title": "Example gaming channel",
      "summary": "Fast gaming-news packaging with dense captions.",
      "source_url": "https://www.youtube.com/@example",
      "platform": "youtube",
      "status": "active",
      "tags": ["competitor", "gaming_news"],
      "payload": {
        "observed_patterns": {
          "hook_seconds": 2.0,
          "caption_density": "high"
        }
      },
      "provenance": {
        "collector": "orion",
        "method": "manual_research",
        "confidence": 0.94
      },
      "observed_at": "2026-09-30T16:00:00Z"
    }
  ]
}
```

## Provenance

`provenance` should describe where a claim came from, not merely repeat the
record payload. Useful fields include:

- collector or producer
- research/search method
- source IDs or URLs
- confidence
- captured/published timestamps
- whether a value is observed, calculated, or inferred

Do not place credentials, tokens, cookies, or other secrets in intelligence
payloads or provenance.

## Reading retained intelligence

List channel records:

`GET /v1/channels/{channel_profile_id}/intelligence-records`

Optional filters:

- `record_kind`
- `status`
- `limit`

Fetch one record:

`GET /v1/channels/{channel_profile_id}/intelligence-records/{record_id}`

The channel path is enforced against the record's stored channel identity.

## Katcha AI grounding

`intelligence_record` is a supported Katcha AI typed resource. A stored record
can be attached to a Command Center request exactly like a clip, publication, or
trend opportunity. Katcha resolves the record directly from its database and
passes its summary, payload, provenance, timestamps, and source information into
the grounded evidence context.

This keeps the handoff path explicit:

1. external research produces a durable batch;
2. Katcha stores and audits the batch;
3. stable records accumulate updates over time;
4. Katcha AI can consume those records as first-party channel context;
5. later automation can build on the same records instead of starting over.

## Handoff inbox

The Handoff Inbox is a file transport into the same ingestion service described
above. It exists for workflows that can create a Katcha batch but cannot reach
the local Katcha API directly.

Open **Sources → Handoff inbox** to import a JSON batch. Katcha uploads the file,
validates it with the same `IngestIntelligenceBatchRequest` contract used by
`POST /v1/intelligence-ingest/batches`, and immediately commits valid records
through `ingest_intelligence_batch`.

For local automation, place files directly in:

```text
handoff/incoming/
```

Then choose **Process pending** in the UI or call:

```text
POST /v1/intelligence-ingest/inbox/process
```

The launcher creates the host directories before Docker starts, and the API
mounts `./handoff` at `/handoff`. The directory is ignored by Git.

### File lifecycle

- `incoming/`: files waiting to be validated and imported.
- `processed/`: successfully imported files.
- `failed/`: rejected files retained for diagnosis or correction.
- `receipts/`: machine-readable outcomes containing the batch ID, counts,
  record IDs, replay state, or validation error.

Files are capped at 10 MiB. Filenames must be simple `.json` names; path
components and symlinked local-drop files are rejected.

Uploading the same filename with identical bytes is idempotent. Uploading the
same filename with different bytes fails closed. The underlying
`channel_profile_id + batch_key` hash guard still applies, so a differently
named file cannot silently mutate a previously committed batch.

A successful handoff only stores intelligence. It does **not** publish content,
bypass rights review, start production, or authorize uploads.
