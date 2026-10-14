# Online workspace parity incident

Live read-only inspection on 2026-10-14 at 04:27 UTC confirmed that
`app.katcha.stream` was serving `b2ae78fc911c1d564db13b2606de1c299404a5ae`
(October 7). At inspection, accepted main was
`99c451187a954c5c5d5526a1cd02a478e3134942`: 70 first-parent commits ahead.
The primary was healthy and authoritative at deployment epoch 4. PostgreSQL
was still at `0057_editorial_image_lineage`. The public root, `channels.css`
and release identity endpoint returned JSON HTTP 404.

## Causes and fixes

1. Runtime publication does not promote production. Compose uses the exact
   `KATCHA_RELEASE_SHA` in `/etc/katcha/katcha.env`; new main images never change
   that file. The host therefore lacks the newer Media Bin, source preparation,
   measured-shot and upload-recovery changes. See
   [production pipeline](PRODUCTION_DEPLOYMENT_PIPELINE.md).
2. The old API does not serve root-absolute workspace assets. Local fixture
   servers hid this discrepancy. Accepted PR #399 fixes real API asset routing;
   #405 restores the domain-root home route, and #407 adds release identity.
3. Even current main's hosted consolidated production worker omitted Editorial
   and SceneIndex workflows registered by the standalone local longform worker.
   Both entry points now use one shared longform-worker factory; the hosted
   process also manages the existing Editorial reconciliation loop. No additional
   competing worker or queue is introduced.
4. Hosted API/production images lacked media decoding/probing prerequisites.
   PyAV is now a core dependency; control and production images include FFmpeg.
   The image pipeline tests the built images with synthetic video, real decoding
   and ffprobe. Control-image acceptance also requests every primary workspace
   and its linked assets through the actual FastAPI router. These checks run with
   networking disabled and skip application startup handoff processing.

## Required supervised rollout

Image checks and this source fix do not authorize a production rollout. Before
changing the existing primary:

- Require successful exact-main CI and the entire image publication workflow;
  use `scripts/check_production_release.py EXACT_SHA`. Stage the four running
  application images and verify arm64 architecture, revision labels and digests.
- Recheck the pinned old SHA, clean host checkout, durable mount, active identity,
  backup/restore freshness and existing workflow inventory. The incident audit
  observed ten running workflows; their existing workflow implementations are
  unchanged by this fix. Stop old workers gracefully before starting replacements.
- Retain the old images, checkout history and protected environment configuration.
  Obtain a fresh consistent backup before applying additive revisions 0058–0063.
  Never downgrade/drop search evidence or replace production data with local data.
- Update only the release pin and application checkout; retain deployment ID,
  epoch, provider policy and recovery/spend settings. Restart the existing service
  under a bounded maintenance window. Verify schema head, all worker registrations,
  API/renderer readiness, leadership identity and public release attestation.
- Verify every public workspace and linked asset, including the Editorial media
  preparation, scene-index and shot-handoff scripts, against the deployed source.
  Confirm their bytes, content types and browser layout; HTTP readiness alone is
  insufficient. Stop for failures instead of declaring the incident resolved.

Before migrations, rollback can restore the old release/configuration. After
migrations, retain the new evidence tables and use only a reviewed compatible
application rollback; never automatically downgrade the database.

Source-search dispatch and optional semantic matching remain separately gated.
Revision 0063 creates uninitialized evidence tables. It does not fabricate a new
ledger or activate provider traffic. Follow
[search-ledger recovery](SEARCH_LEDGER_RECOVERY.md) for any retained evidence
import and completeness/activation review. Runtime/UI parity does not prove live
providers, rights clearance or production media acceptance.
