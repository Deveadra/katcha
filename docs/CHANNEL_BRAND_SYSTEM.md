# Channel Brand Operating System

Status: foundation for issue #16

Katcha treats a channel brand as a versioned production contract, not a mood board. A brand version must be specific enough that two independent workers can generate scripts, narration, captions, renders, packaging, and engagement prompts that feel like the same channel, while preserving enough structured metadata to learn what actually works.

## Product principle

The brand exists to improve four things at once:

1. **Recognition** — a viewer should recognize the channel before reading the handle.
2. **Connection** — the host should feel familiar, honest, and conversational rather than synthetic or promotional.
3. **Comprehension** — captions, motion, and commentary should make the source easier and more enjoyable to follow, not noisier.
4. **Learning** — meaningful creative treatments must have stable identifiers so analytics can compare outcomes without pretending correlation is causation.

YouTube's current recommendation guidance is consistent with this model: content performance is framed around appeal, engagement, and satisfaction; the opening needs to deliver on the package; and Shorts community guidance emphasizes two-way interaction instead of treating viewers as passive traffic.

Research references:

- https://support.google.com/youtube/answer/16559650
- https://support.google.com/youtube/answer/11914225
- https://blog.youtube/creator-and-artist-stories/grow-youtube-channel-interactive-shorts/
- https://blog.youtube/culture-and-trends/gen-z-creators/

## Brand-version contract

A channel brand version should eventually persist the following fields. The names below are intentionally renderer/provider neutral.

```json
{
  "brand_key": "channel_01",
  "version": 1,
  "persona": {
    "key": "youth_host",
    "version": "2.0.0"
  },
  "voice_policy": {
    "direction_key": "grounded_teen_v1",
    "preferred_profiles": ["openai_youth_v2", "gemini_youth_v2"]
  },
  "visual": {
    "theme_key": "signal_v1",
    "caption_treatment": "impact_clean_v1",
    "motion_treatment": "restrained_punch_v1",
    "end_card_treatment": "verdict_v1"
  },
  "packaging": {
    "title_family": "specific_curiosity_v1",
    "thumbnail_family": "single_focus_v1"
  },
  "interaction": {
    "allowed_rituals": [
      "official_ruling",
      "pick_a_side",
      "prediction",
      "comment_callback"
    ]
  },
  "experiment_metadata": {
    "cohort": "launch_baseline"
  }
}
```

The stored production must record the exact brand version selected at creation time. Regeneration preserves that version unless a deliberate rebrand operation is requested.

## Creative layers

### 1. Host identity

The host is not a generic narrator and not an exaggerated streamer. The persistent archetype is:

> The funny friend who noticed the exact thing you noticed half a second before you did.

The host should be energetic enough to move the Short forward, but grounded enough to feel credible. The emotional baseline is amused curiosity, not permanent amazement.

The host may be:

- amused;
- skeptical;
- mock-serious;
- impressed;
- confused in a precise way;
- lightly competitive;
- briefly sincere when the source earns it.

The host should not default to:

- yelling;
- fake shock;
- fake outrage;
- fake disbelief;
- constant slang;
- ad-read excitement;
- generic praise;
- narrating visible action;
- treating every clip as "insane", "crazy", or "wild".

### 2. Hook grammar

Hooks are promises, not decorations. The first host line or first visual treatment should do at least one of these jobs:

- reveal the social question: "Who is actually wrong here?";
- frame a prediction: "There is exactly one way this can end.";
- identify the hidden detail: "Watch the guy in the back.";
- establish mock stakes: "This has somehow become a legal proceeding.";
- create a precise curiosity gap: "The second attempt is where this stops making sense.";
- skip narration entirely when the source already has a strong native hook.

A hook must never promise a reveal the clip does not contain.

### 3. Commentary density

The source remains the star. Commentary is successful when removing it would make the clip meaningfully less funny, less clear, or less participatory.

Baseline rules:

- Prefer one strong framing line over three filler reactions.
- Prefer pre + post narration.
- Mid-clip interruption requires a specific reason: punchline timing, confusion prevention, prediction, or a callback.
- Do not speak over the source's best line unless the edit is intentionally replacing it.
- Silence is an available creative tool.

### 4. Humor grammar

Preferred tools:

- understatement;
- precise comparisons;
- mock bureaucracy / mock analysis;
- fake seriousness;
- callbacks;
- running scorecards;
- gently escalating logic;
- dry labels;
- light sarcasm without cruelty.

Avoid humor that depends on humiliating a private person, punching down, invented facts, or sounding like an adult trying to imitate current teenage slang.

### 5. Audience interaction

Interaction should create a social action, not ask for a metric.

Good interaction prompts let viewers:

- judge;
- rank;
- predict;
- choose a side;
- add an example;
- settle a recurring channel rule;
- challenge the host's ruling.

Bad interaction prompts are generic requests to like, subscribe, follow, comment, or "let me know what you think" when no specific decision exists.

## Recurring audience rituals

Rituals are reusable but optional. They should appear only when the source naturally supports them.

### `official_ruling`

The host gives a mock-formal verdict after the payoff. The audience can overrule it in comments.

Examples of structure:

- "Official ruling: technically impressive, spiritually unacceptable."
- "The committee gives this a 7.4. Appeal below."

The exact words should vary; the ritual identifier remains stable.

### `pick_a_side`

The clip naturally contains two defensible sides, choices, or reactions. The host frames the specific disagreement and exits.

### `prediction`

Used only when the outcome is not already spoiled by the opening frame/title. The prediction should happen before the relevant reveal and should not require withholding obvious information dishonestly.

### `comment_callback`

A real prior comment, question, running joke, or audience ruling becomes part of a future Short. This is the strongest candidate for building channel lore because it proves the channel is listening.

### Naming the audience

Do not manufacture a fandom name at launch. If recurring language emerges organically from comments, it can be evaluated later and versioned deliberately.

## Visual system principles

The launch renderer should feel expressive and contemporary without looking like neon gamer UI, children's content, or an over-edited template pack.

Required behaviors:

- high text contrast;
- readable mobile safe zones;
- no permanent visual clutter;
- one primary point of emphasis at a time;
- motion tied to meaning or comedic timing;
- captions that remain readable over unpredictable source footage;
- restrained use of accent colors;
- no random emoji confetti;
- no visual element should exist solely because "Shorts need movement".

The brand token contract should own visual choices. React components should render tokens, not invent the brand through hard-coded constants.

## Caption grammar

Captioning is a designed reading experience, not a dump of transcription text.

Launch defaults:

- narration captions only by default;
- 2–5 spoken words per visual chunk when timing permits;
- maximum two visual lines;
- sentence case by default;
- uppercase reserved for very short emphasis, not whole sentences;
- punctuation kept when it helps comedic timing or comprehension;
- one emphasized word/phrase at a time;
- captions placed above platform controls and away from faces when detectable;
- no karaoke-style word flashing unless a measured experiment justifies it;
- never cover the source's own critical text.

## Packaging system

Packaging is part of the promise the video must fulfill.

### Title families

`precise_curiosity`
: Identifies a specific strange decision/detail without revealing the payoff.

`social_verdict`
: Frames a genuine disagreement the viewer can resolve.

`escalation`
: Signals that the sequence changes meaningfully later.

`hidden_detail`
: Directs attention toward a non-obvious detail that is actually present.

Avoid generic superlatives, misleading stakes, and keyword stuffing.

### Thumbnail / cover grammar

When a surface uses a thumbnail:

- one focal subject or conflict;
- one readable visual idea;
- no collage unless the comparison itself is the idea;
- minimal text, generally 0–4 words;
- use the brand accent as a directional/highlight device rather than a full-frame wash;
- avoid repeating the title verbatim.

## Experiment identifiers

Every meaningful creative change should have a stable treatment key. Initial dimensions:

- `brand_version`
- `hook_family`
- `host_style`
- `narration_density`
- `caption_treatment`
- `motion_treatment`
- `interaction_ritual`
- `recurring_bit`
- `packaging_family`
- `content_category`

These fields are evidence features. They are not causal claims.

## Success metrics

Raw starts/views are insufficient by themselves. Since YouTube changed view counting in August 2026 so a view starts when playback begins, Katcha should continue to prioritize metrics that better reflect real consumption and value.

Useful outcomes include:

- chose-to-view / swipe behavior where available;
- engaged views / qualified views where available;
- average view duration;
- average percentage viewed;
- retention at the opening and payoff;
- rewatches / retention spikes;
- likes and shares;
- comments per engaged viewer;
- returning viewers;
- subscribers attributed to content;
- click-through / continuation into long-form;
- revenue and contribution margin.

No single metric is the brand objective. The brand should increase the probability that the right viewers choose, stay, enjoy, participate, and come back.

## Brand evolution rule

Do not let a model silently rewrite the brand because three Shorts performed differently. A brand change requires:

1. a new version;
2. a stated hypothesis;
3. enough comparable evidence to justify testing;
4. an operator-visible diff;
5. reversible rollout.

Katcha may recommend a treatment experiment, but identity changes remain explicit and versioned.
