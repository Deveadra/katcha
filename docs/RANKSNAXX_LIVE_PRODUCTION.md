# RankSnaxx first real production

This runbook is the first controlled production path for real RankSnaxx media after
the synthetic acceptance harness is healthy.

Unlike `scripts/ranksnaxx_private_acceptance.py`, this runner does **not** create
synthetic fixtures or manufacture rights assessments. It only activates a Katcha
trend opportunity when the existing activation service reports enough real clips that
are already:

- promoted from discovery;
- ingested and normalized;
- analyzed and scored;
- backed by a production-eligible latest rights assessment; and
- frozen into a qualified trend evidence packet for the RankSnaxx channel.

The first YouTube upload is hard-limited to `private` with subscriber notifications
disabled.

## Preconditions

The normal local stack must be healthy and the existing RankSnaxx channel profile must
still be connected to YouTube.

The deployment must also have:

- `KATCHA_AI_ENABLED=true`;
- at least one configured AI provider key;
- an active RankSnaxx brand and editing blueprint;
- a current RankSnaxx trend watch profile;
- at least one activation-ready trend opportunity with enough eligible real clips.

The runner deliberately refuses the synthetic `acceptance-media` fixture URLs.

## 1. Let Katcha choose the best activation-ready opportunity

Run without either approval flag first:

```bash
python scripts/ranksnaxx_live_production.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID
```

Katcha inspects the current ranked opportunities in server order and selects the first
one whose activation preview is ready for a five-item RankSnaxx episode. It activates
that opportunity, freezes the trend evidence and media lineage, and runs the real AI
script and TTS stages.

The command stops at the editorial review boundary and prints the `episode_id`.

To target a specific qualified opportunity instead:

```bash
python scripts/ranksnaxx_live_production.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID \
  --opportunity-id OPPORTUNITY_ID
```

## 2. Render the reviewed episode

After inspecting the editorial state, resume the same episode:

```bash
python scripts/ranksnaxx_live_production.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID \
  --episode-id EPISODE_ID \
  --approve-render
```

This approves the editorial gate, runs the production Remotion render, waits for render
verification, and stops before YouTube publication.

Inspect the verified render in the Editing Control Center before proceeding.

## 3. Upload the same verified render privately

Only after visual/audio review:

```bash
python scripts/ranksnaxx_live_production.py \
  --channel-profile-id YOUR_CHANNEL_PROFILE_ID \
  --episode-id EPISODE_ID \
  --approve-private-upload
```

The runner revalidates real trend/discovery lineage before publication and creates a
YouTube publication with:

```text
privacy_status=private
notify_subscribers=false
publish_at=null
contains_synthetic_media=false
```

Success prints the Katcha publication ID and the real YouTube video ID.

## What the runner will not do

It will not:

- assign or infer media rights;
- promote an unqualified trend opportunity;
- bypass either RankSnaxx review gate;
- use the synthetic acceptance fixture service;
- publish unlisted or public;
- notify subscribers; or
- raise the channel automation level.

If no opportunity is activation-ready, the command fails with the readiness reason
rather than filling the episode with weaker or unqualified media.
