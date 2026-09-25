# RankSnaxx private acceptance

This is the controlled live acceptance path for the first Katcha channel. It uses
five locally generated synthetic clips, so the test can exercise the production
pipeline without depending on third-party media or bypassing Katcha's rights gate.

The acceptance path is:

```text
synthetic fixtures
  -> discovery candidates
  -> owned/original/cleared rights assessments
  -> promotion + ingest
  -> analysis/scoring
  -> RankSnaxx five-item episode planning
  -> AI script + TTS
  -> editorial review
  -> Remotion render + verification
  -> render review
  -> PRIVATE YouTube upload
```

It never requests `public` or `unlisted` privacy. The upload flag is deliberately
explicit.

## Prerequisites

The normal local stack must already be healthy, RankSnaxx must have a live YouTube
connection and channel profile, and `KATCHA_AI_ENABLED=true` with at least one AI
provider key.

Start the fixture-media service alongside the existing stack:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.discovery.yml \
  -f docker-compose.trends.yml \
  -f docker-compose.intelligence.yml \
  -f docker-compose.acceptance.yml \
  up -d acceptance-media
```

Verify it:

```bash
curl -fsS -o /dev/null http://localhost:8090/clip-1.mp4 && echo "acceptance media ready"
```

## Render acceptance first

Run without upload approval first. This performs discovery, rights qualification,
ingest, analysis, AI scripting/TTS and stops at the review boundary:

```bash
python scripts/ranksnaxx_private_acceptance.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID
```

## Controlled private upload

When you intentionally want the full live acceptance, use a new run key and the
explicit private-upload approval:

```bash
python scripts/ranksnaxx_private_acceptance.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID \
  --run-key live-private-001 \
  --approve-private-upload
```

The runner approves only the synthetic acceptance episode, then creates a publication
with `privacy_status=private`, `notify_subscribers=false`, and
`contains_synthetic_media=true`. Success prints the Katcha publication ID and the
real YouTube video ID.

Do not promote the channel automation level for this test. RankSnaxx should remain at
`review_required` until real evidence supports an automation promotion.
