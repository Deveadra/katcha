# Operational recovery audit

This recovery addresses broken connections between existing Katcha subsystems.
The draft PR is the checkpoint for code and evidence; it does not certify an
operator's installed credentials or live provider access.

## Findings and repairs

| Failure | Cause found in the repository | Repair |
| --- | --- | --- |
| A connected subscription could not execute production work | Codex was wired into conversation/planning, while scripts and visual analysis still required paid API providers | Shared subscription execution for short/ranked scripts, longform editorial tasks, packaging candidates and Codex contact-sheet vision; API fallback remains available |
| Successful AI inference could appear to fail | Connection testing also required the usage endpoint to work | Separate inference success from optional usage telemetry |
| Responses were rejected or lost | Codex requests omitted instructions; parser relied on text deltas and did not reliably distinguish completed responses from truncated streams | Send instructions; read final response text and tool output; reject incomplete/error streams; normalize network errors |
| Sources and interests were stored but not researched | Saved source polling intervals and channel interest profiles had no automatic dispatcher | Durable, restart-safe research dispatcher in the intelligence worker, using existing discovery workflows, poll leases and provider quotas |
| Topic watch workflows stalled after collection | Consolidated intelligence worker omitted the command-cycle activity registration | Register the activity and test every consolidated workflow's named activity registrations |
| Web research ignored Codex | Web scout tried other routes only | Use subscription-backed web search, retain grounding evidence and validate full page identity including query parameters |
| Research results never became clips | Source results only displayed links; promotion required production clearance even for review | Explicit “Add to Clips for review”, without clearing production rights; block download from blocked/discovery-only sources |
| Downloaded clips were not analyzed | Ingestion workflow ended after download | Versioned analysis dispatch after successful ingestion |
| Channel/acquisition context disappeared on download | Download metadata overwrote Katcha's stored metadata | Preserve authoritative lineage during discovery and ingestion |
| Channel watch listing mixed scopes | Latest version grouping used the watch key alone | Include scope in grouping and joins |
| Running containers concealed unavailable workers | Existing readiness did not inspect all execution queues | Settings system check for database, renderer, dispatcher and eight worker queues; distinguish configured AI from proven inference |

## Operating behavior

Enabled pull sources are collected while Katcha runs. Sources can opt out of
automatic research or be paused. Channel interests generate channel-scoped
research using the channel's YouTube connection and available web-search provider.
Shared sources remain unassigned. Existing data and source records are retained.
`KATCHA_RESEARCH_ENABLED=false` disables automatic research globally.

Review downloads are not production approval. Rights, audio and originality
checks remain required for production. Subscription text/image generation does
not provision paid API credentials. Speech and native full-video analysis retain
their existing separate provider requirements.

## Verification and limits

- Initial draft checkpoint passed all CI jobs: PostgreSQL-backed Python suite,
  migrations, browser journeys, renderer/FFmpeg, ranked render through HTTP/S3,
  Temporal smoke, storage image, acceptance media and infrastructure validation.
- Added recovery tests exercise a synthetic saved source through real persistence,
  source history and trend bridging; channel separation; pause races; malformed
  source isolation; review download lineage; subscription execution and error
  behavior; packaging replay; missing worker detection.
- Launcher full-startup acceptance now checks execution workers after cold and
  warm startup, in addition to GUI/API readiness and volume-preserving shutdown.
- Provider replies and discovery media in automated tests are fixtures. No live
  user account inference, speech generation, discovery or publication has been
  performed in this workspace. Settings “Test response” verifies actual account
  inference; “Check all systems” diagnoses the running installation.
- Final status and the precise tested commit are recorded on the PR. Keep the PR
  in draft until the final changes pass CI and outstanding operational checks are
  understood. A passing repository suite is not proof of all live providers.
