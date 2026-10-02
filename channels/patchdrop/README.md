# PatchDrop — Channel Identity

Status: Brand v1 launch candidate  
Channel key: `patchdrop`

## Brand promise

Gaming news, updates, discoveries, and player intelligence filtered down to what actually matters.

## Audience

Players who want to stay current without watching hours of filler. The channel is long-form-first with Shorts used for discovery, fast updates, and hook testing.

## Voice

A knowledgeable gaming friend: sharp, relaxed, lightly funny, never shouty, never corporate, never fake-hyped.

No human presenter appears on camera. Identity comes from narration, writing, packaging, motion, sound, and repeatable editorial products.

## Core CTA ritual

Primary:
> Don't miss the next drop — subscribe.

Alternates:
- Never miss a drop — subscribe.
- If this helped, catch the next drop — subscribe.
- Stay patched in — subscribe.

The CTA should be brief, low-pressure, and placed after value has already been delivered. It may vary by content product, but the "drop" language should remain recognizable.

## Content products

- `breaking` — 4–7 min: important news/update while interest is accelerating
- `deep_dive` — 8–14 min: why a major development matters
- `you_missed_this` — 7–12 min: hidden trailer/showcase/details
- `player_intel` — 6–12 min: builds, mechanics, secrets, updates, utility
- `radar` — 10–15 min: upcoming/under-covered games worth knowing
- `short_signal` — 20–45 sec: one fast story, fact, change, or discovery

## Repository / storage contract

```
channels/patchdrop/
├── README.md
├── brand/
│   └── brand.v1.json
├── assets/
│   └── manifest.v1.json
├── packaging/
├── products/
├── voice/
└── references/
```

Git stores:
- brand contracts
- edit/product rules
- copy systems
- thumbnail/layout specs
- small SVG/JSON assets
- asset manifests
- version history

Object storage stores:
- rendered logos
- raster thumbnails
- WAV/MP3 voice references
- music/stingers
- large source media
- rendered examples
- fonts/packages when licensing and deployment require them

Every object-store asset must be referenced through the manifest with an immutable key, checksum, media type, and brand version.

## Visual direction

Dark editorial gaming identity, not neon-gamer clutter.

- near-black base
- warm white typography
- electric lime as the primary "drop" signal
- blue for informational structure
- coral only for warnings / conflict
- one visual point of emphasis at a time

Primary logo concept: a digital patch tile with one square visibly "dropping" from the grid. The wordmark is compact and technical, not esports-styled.

## Launch rule

Do not publish publicly until:
1. handle/name sweep is complete;
2. logo and thumbnail system survive actual rendered samples;
3. one long-form pilot and one Short pilot render cleanly;
4. Brand v1 is frozen and ingested into the channel profile.
