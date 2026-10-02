# Manual content workflow

`/content?channel=<profile>` connects file, public link and existing clip intake to
production and publication review. Clips exposes **Add content** and **Create video**;
Channel Studio and Sources link to the same history. Publication links select the
correct video and Content tab. A linked clip opens its channel-scoped library record.

## Contracts

- Each request has a persistent, channel-scoped receipt and a unique request key.
  URL retries reuse acquisition identity. File intake accepts MP4, WebM, MOV and MKV
  exports up to 4 GiB in chunks of at most 8 MiB. Offset, size and chunk hashes are
  checked; a replay cannot replace acknowledged bytes. Completion verifies real
  video shape with ffprobe and deduplicates the media by SHA-256. Staging chunks are
  deleted only after verified media commits; failed cleanup can retry `/complete`.
- Intake requires explicit permission to publish the media and audio. Existing
  managed acquisition rights gates continue to apply. Standing trailer permission
  records audio clearance rather than claiming original authorship; monetization
  eligibility and originality evidence remain separate facts.
- **Use video unchanged** creates an approved source passthrough without an AI
  provider. Optional AI shorts retain the existing AI execution and rights checks.
  **Review for YouTube** creates a held private draft and starts no upload.
- Channel Studio edits title, description, tags, custom JPEG/PNG thumbnail (2 MiB),
  subscriber notifications, audience designation and synthetic-content disclosure.
  Final visibility can be private, unlisted, public when ready or scheduled public.
  Times use the channel timezone. Missing/ambiguous daylight-saving times are rejected;
  API callers may supply an absolute `publish_at` with an explicit offset.
- Draft edits survive refresh, selection and request failures in the current browser
  tab. Metadata mutations increment a version; saving and starting check it under a
  row lock. Start saves the visible form first. Explicitly discarding edits reloads
  the current saved plan. Once transfer starts, metadata is locked.
- New manually reviewed scheduled plans default to keeping the video private if
  processing misses the release time. Publishing immediately is an explicit alternative.
  Completed uploads expose release controls. A provider read-back confirms changes;
  an uncertain mutation blocks another change until reconciliation reads actual state.
  Thumbnail failure retains the known video ID for existing upload recovery.
- Activity refreshes every eight seconds while visible. Selected publication state
  refreshes independently of editable fields. Failed events retain failure labels.
  Panel failures do not suppress healthy panels. Content, production, publication
  and package history have pagination; byte progress reports acknowledged bytes.
- Recurring source checks reuse the existing `AutomaticResearchWorkflow`. Source controls
  expose this dispatcher and its runtime availability. New guided sources default to manual checks;
  existing recurrence preferences remain intact. The explicit toggle and interval
  govern the existing dispatcher. Pausing a source prevents new discovery work;
  already-running checks may finish. Recurrence discovers finds for review and
  does not grant publication authorization.

## Rollout and boundaries

Apply migration `0048_manual_content` before running the updated API. Keep the normal
acquisition, production, publishing and research workers running. No new worker or
external connector is required. Existing channel-associated sources receive content
receipts when their content history is opened; original media and events are retained.

Source packages and InVideo handoffs retain their existing entry points. Cloud-drive
and authenticated sources, recordings, editor projects, image sequences and audio
can enter through an authorized video export. This PR does not add cloud sync,
project-file rendering or every vendor integration. Captions, playlists, cards, end
screens, premieres and advanced YouTube checks remain available through the explicit
YouTube Studio link. These are visible boundaries, not implied completed capabilities.
