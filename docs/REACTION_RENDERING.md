# Shared reaction overlays (R1)

This is a shared Katcha rendering feature. It has no RankSnaxx-specific rendering code. Both `Short` and `RankedEpisode` accept `reaction_events` in their existing v1 manifests; old manifests with no such field continue to render with an empty track.

## Register one channel-owned pack

Store transparent PNGs in S3/MinIO under immutable, versioned keys. Example for RankSnaxx:

```json
{
  "brand_key": "ranksnaxx",
  "pack_key": "host_emotes",
  "version": 1,
  "assets": {
    "meme_cry": {
      "storage_key": "brands/ranksnaxx/reactions/host_emotes/v1/meme_cry.png"
    },
    "sneaky": {
      "storage_key": "brands/ranksnaxx/reactions/host_emotes/v1/sneaky.png"
    }
  }
}
```

The pack is stored at `brand_snapshot.visual.reaction_pack` by the brand-version contract, not in the shared React code. It must match `brand_snapshot.visual.brand_key`. The implementation validates the versioned path; **the object store does not enforce write-once by itself**. Never overwrite a published key: create `v2/` for replacement PNGs and activate a new brand version. Uploading the PNGs is a separate operator step; this PR does not supply art, a pack-management UI or an upload API.

## Author a reaction against actual voiceover timing

For a standard Short, put this on the selected production script's `script_metadata.reaction_cues`. `line_ref` is the zero-based narration segment index.

For a countdown `RankedEpisode`, put the same array on the selected episode script's `script_payload.reaction_cues`. `line_ref` is the narration asset's `sequence` (not countdown position).

```json
[
  {
    "id": "ice-cream-meme-cry",
    "asset_key": "meme_cry",
    "line_ref": 0,
    "offset_seconds": 0.1,
    "duration_seconds": 1.4,
    "anchor": "bottom_right",
    "animation": "pop_bounce",
    "scale": 0.22
  }
]
```

The builder uses the **scheduled** narration start, after overlap resolution, so `offset_seconds` is relative to where the line actually plays. The manifest stores absolute `start_seconds`, selected `storage_key`, `brand_key`, `pack_key` and `pack_version`. The renderer only executes that frozen timeline. If a pack or referenced line is absent, the asset belongs to another channel, or a cue exceeds output duration, manifest creation fails. No sentiment inference occurs at render time.

An already-frozen render manifest is reused. To change an existing video's cues, use an explicit new render generation/output key rather than editing the original artifact in place; the current first-generation worker does not include a standalone cue-editing UI or regenerate action.

## What the shared renderer does

`renderer/src/reaction-track.jsx` converts seconds to frames, mounts a transparent PNG with `Img`, and plays `pop_bounce`, `fade` or `slide`. It supports four corners and sizes proportional to canvas width. Bottom anchors are lifted above the caption band, and top anchors leave headroom for countdown badges; these are deterministic layout heuristics, not object-detection or guaranteed content-aware collision avoidance.

The renderer checks the exact object exists, presigns its URL using the existing S3/MinIO client, and fails the render if it is missing. Neither composition refers to a specific brand or asset name. The existing caption track remains above reactions, preserving caption legibility.

## Verification

`pytest -q tests/test_reaction_rendering.py tests/test_manifest.py tests/test_ranked_episode_rendering.py` exercises timing, channel isolation, stable line references, legacy manifests and invalid paths. The GitHub renderer CI bundles the production compositions and runs `node test/reaction-smoke.mjs`. The smoke uses a **generated, test-only transparent PNG**, a synthetic source video, and silent WAV to exercise the actual `ShortVideo` and `RankedEpisodeVideo` components. For each composition it compares screenshots at three timestamps with reactions on/off: frames must match before and after the cue and differ during it. It also encodes and probes both H.264 MP4s. On successful CI runs, the `synthetic-reaction-previews` GitHub Actions artifact contains both videos and key screenshots, retained for 7 days. These fixtures are not proposed brand artwork or a test of real voice synchronization. A preview with final character PNGs and real source/voice media is still required to approve actual visual placement, caption collision, animation rhythm and mobile platform UI zones.
