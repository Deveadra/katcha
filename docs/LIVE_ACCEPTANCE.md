# Live acceptance runbook

This runbook proves the local Katcha stack can move real media bytes through ingestion,
analysis, AI editorial, rendering, review, encrypted YouTube publishing, and analytics
without making anything public.

The acceptance media is synthetic and generated locally. It is deliberately unsuitable
for channel launch content; its only purpose is to validate the system safely before
using rights-qualified editorial media.

## Preconditions

- the complete local stack is healthy;
- `KATCHA_AI_ENABLED=true`;
- at least one AI provider key is configured;
- a YouTube connection exists and is active;
- a Katcha channel profile exists for that connection;
- the automation level remains `review_required`.

Load the control token without printing it:

```bash
CONTROL_TOKEN="$(grep '^KATCHA_CONTROL_API_TOKEN=' .env | cut -d= -f2-)"
```

## Start synthetic acceptance media

Add the acceptance overlay to the already-running project:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.discovery.yml \
  -f docker-compose.trends.yml \
  -f docker-compose.intelligence.yml \
  -f docker-compose.acceptance.yml \
  up -d acceptance-media
```

Wait for:

```text
katcha-acceptance-media-1 ... (healthy)
```

The five internal URLs are:

```text
http://acceptance-media:8090/clip-1.mp4
http://acceptance-media:8090/clip-2.mp4
http://acceptance-media:8090/clip-3.mp4
http://acceptance-media:8090/clip-4.mp4
http://acceptance-media:8090/clip-5.mp4
```

They are reachable from Katcha's Docker network and do not need to be published to the
internet.

## Acceptance boundary

The production acceptance must stop for operator review after rendering. The operator
must inspect the generated render before any publication is registered.

If a publication is created, use:

```json
{
  "privacy_status": "private",
  "notify_subscribers": false,
  "made_for_kids": false
}
```

Do not use `unlisted`, `public`, or `publish_at` for this acceptance run.

A successful acceptance proves:

1. source media can be ingested and normalized;
2. analysis/scoring completes;
3. channel-scoped RankSnaxx editorial state is frozen into the production lineage;
4. AI script/TTS execution completes within budget;
5. Remotion output passes post-render verification;
6. human review remains in control;
7. the encrypted OAuth connection can perform a private YouTube upload;
8. YouTube assigns a video ID and Katcha persists it without exposing provider secrets.

The synthetic service can be stopped without deleting any Katcha volumes:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.acceptance.yml \
  stop acceptance-media
```
