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

The normal local stack must already be healthy and RankSnaxx must have a live YouTube
connection and channel profile.

For normal development acceptance, use `KATCHA_AI_ENABLED=true` with fixture mode.
No OpenAI or Gemini key is required by the editorial/voice path:

```env
KATCHA_AI_ENABLED=true
KATCHA_AI_EXECUTION_MODE=fixture
```

Development `auto` mode also resolves to fixture mode. The runner prints the backend
AI runtime state before starting, including whether external provider calls are enabled.

Use `KATCHA_AI_EXECUTION_MODE=live` only for an intentional provider smoke test or
production acceptance.

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

## Editorial acceptance first

With no approval flag, the runner performs discovery, rights qualification, ingest,
analysis, scripting/TTS and stops at the editorial review boundary. In fixture mode
the analysis/script responses are deterministic and narration is generated locally,
so this stage has zero external AI/TTS cost:

```bash
python scripts/ranksnaxx_private_acceptance.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID
```

## Real render acceptance

Resume the `episode_id` printed by the editorial acceptance and explicitly approve
that same synthetic fixture for rendering. This avoids a second AI/TTS pass,
exercises the real RankSnaxx renderer, and stops at the render-review boundary;
nothing is uploaded to YouTube:

```bash
python scripts/ranksnaxx_private_acceptance.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID \
  --episode-id EPISODE_ID_FROM_THE_PREVIOUS_RUN \
  --approve-render
```

## Controlled private upload

When you intentionally want the full live acceptance, resume the same rendered
episode and pass the explicit private-upload approval:

```bash
python scripts/ranksnaxx_private_acceptance.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID \
  --episode-id EPISODE_ID_FROM_THE_PREVIOUS_RUN \
  --approve-private-upload
```

You can also run the entire path in one command by omitting `--episode-id` and adding
`--approve-private-upload`; the staged workflow above is preferred because it makes
both review boundaries visible and avoids repeating paid AI/TTS work.

The runner approves only the synthetic acceptance episode, then creates a publication
with `privacy_status=private`, `notify_subscribers=false`, and
`contains_synthetic_media=true`. Success prints the Katcha publication ID and the
real YouTube video ID.

Do not promote the channel automation level for this test. RankSnaxx should remain at
`review_required` until real evidence supports an automation promotion.
