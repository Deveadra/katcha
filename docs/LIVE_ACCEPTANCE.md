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
- for zero-cost development acceptance, `KATCHA_AI_EXECUTION_MODE=fixture`;
- for an intentional live-provider acceptance, `KATCHA_AI_EXECUTION_MODE=live` and
  at least one AI provider key is configured;
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
4. script/TTS execution completes in the selected execution mode and, in live mode,
   remains within budget;
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


## Final RankSnaxx Brand v2 real-media visual acceptance

Issue #36 stays open until Brand v2 is reviewed against an actual stored
`ranked-episode-render-v1` episode. Synthetic renderer smoke evidence is not enough.

First create/render a real RankSnaxx episode with
`scripts/ranksnaxx_live_production.py` and stop at render review. Keep its
`episode_id`. Then run:

```bash
python scripts/ranksnaxx_brand_v2_acceptance.py \
  --channel-profile-id <RANKSNAXX_CHANNEL_PROFILE_ID> \
  --episode-id <REAL_RANKED_EPISODE_ID>
```

The command:

1. confirms the target channel is RankSnaxx;
2. reuses an existing staged Brand v2 or stages the built-in v2 candidate;
3. rejects synthetic `acceptance-media` source lineage;
4. requires the episode's frozen `ranked-episode-render-v1` manifest;
5. chooses a real narration sequence (or accepts `--line-ref`);
6. renders `meme_cry` through the existing staged-brand preview workflow;
7. waits for normal post-render verification;
8. prints the private preview media URL and leaves Brand v2 inactive.

Watch the verified MP4 in the Editing Control Center or through the authenticated
media endpoint. Check:

- reaction placement is mobile-safe;
- captions remain readable and unobstructed;
- the reaction enters/exits on the intended spoken beat;
- animation rhythm feels natural with the real voice/source timing;
- the overlay stays clear of common platform UI zones.

Do **not** activate v2 unless that real-media preview is accepted.

After visual acceptance, activation is a separate explicit command and requires the
specific verified preview ID:

```bash
python scripts/ranksnaxx_brand_v2_acceptance.py \
  --channel-profile-id <RANKSNAXX_CHANNEL_PROFILE_ID> \
  --activate-reviewed-preview <VERIFIED_PREVIEW_ID> \
  --confirm-visual-acceptance
```

The activation mode re-reads the preview from Katcha and refuses to continue unless
it is verified, belongs to RankSnaxx Brand v2, is backed by a ranked ShortEpisode,
and contains render-verification evidence. It does not accept an episode ID in the
same invocation, preventing preview creation and activation from collapsing into one
unreviewed action.

Only after this operator-reviewed step is successful should #36 be closed.
