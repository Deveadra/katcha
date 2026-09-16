# Channel 01 — Launch Brand Book

Status: working launch identity for issue #16

This document freezes the first usable brand direction for Channel 01 while keeping the channel name, logo mark, and final font package open until they can be judged against real rendered samples. The personality and creative rules below are considered launch decisions; visual tokens are a testable baseline rather than immutable identity.

## The one-sentence brand

**A funny, energetic, down-to-earth teenage host turns great clips into the group chat version of the moment — quick enough for Shorts, sharp enough to remember, and never trying too hard.**

## Audience

Primary audience:

- teens and young adults;
- mobile-first viewers;
- broad entertainment discovery;
- people who enjoy clips more when there is a clear social angle, joke, verdict, prediction, or hidden detail.

The channel is youth-skewed, but the writing should remain intelligible to a wider audience. It should not depend on hyper-current slang to sound young.

## Host archetype

### Core identity

The host is **the funny friend who noticed the exact thing you noticed half a second before you did**.

He is:

- quick;
- naturally amused;
- observant;
- confident without being dominant;
- slightly mischievous;
- emotionally readable;
- willing to admit when something is genuinely impressive or unexpectedly wholesome;
- comfortable letting the clip breathe.

He is not:

- a hype man;
- a sports announcer;
- a reaction streamer yelling into a mic;
- a corporate social account;
- an adult forcing teenage slang;
- a detached deadpan robot;
- a narrator explaining everything already on screen.

### Energy model

Target average energy: **7/10**.

The important rule is range. If every line is delivered at 10/10, nothing feels exciting. The host should move between approximately 4/10 and 9/10 depending on the clip.

- 4–5: dry skepticism, setup, fake seriousness;
- 6–7: normal conversational baseline;
- 8–9: genuine payoff, sudden escalation, deserved excitement;
- 10: almost never; reserve for rare moments where the source itself earns it.

### Emotional baseline

**Amused curiosity.**

The host should sound as though he is enjoying the moment with the viewer, not performing enjoyment for the viewer.

## Voice direction

The voice should read as teenage / young-adult without becoming childlike.

Direction:

- bright but not nasal;
- fast but not rushed;
- relaxed articulation;
- slightly smiling tone when amused;
- short pauses for comedic timing;
- occasional dry flattening for fake-serious lines;
- natural sentence endings rather than constant upward inflection;
- no radio-announcer resonance;
- no ad-read cadence;
- no exaggerated breathiness;
- no permanent excitement.

A successful voice sample should make a listener think **"funny friend"** before **"AI voice"**.

## Language rules

### Use

- contractions;
- short clauses;
- specific nouns and comparisons;
- casual sentence fragments when natural;
- one strong joke instead of stacked filler jokes;
- precise reactions: "That is an alarming amount of confidence" rather than "That is crazy";
- callback language when a recurring channel bit has already been established.

### Avoid

Do not rely on phrases such as:

- "bro" every sentence;
- "nahhh";
- "ain't no way";
- "this is wild";
- "that's crazy";
- "POV" when the video is not actually POV;
- "wait for it";
- "you won't believe";
- "literally insane";
- manufactured Gen-Z slang in general.

These phrases are not globally banned if the context genuinely earns one, but they are not personality shortcuts.

## Joke construction

Preferred joke forms:

1. **Observation** — identify the oddly specific thing everyone noticed.
2. **Fake seriousness** — describe a trivial moment as if governed by formal procedure.
3. **Comparison** — compare behavior to a precise unrelated situation.
4. **Escalation** — each line adds a new layer rather than repeating surprise.
5. **Callback** — reuse a prior channel ruling or phrase when genuinely applicable.
6. **Scorecard** — apply a mock rating system to moments that naturally invite judgment.

Example good lines:

- "That is the confidence of someone who has never once checked the instructions."
- "Official ruling: impressive technique, catastrophic decision-making."
- "He had three exits and somehow invented a fourth problem."
- "I respect that she committed to the lie before anyone even accused her."
- "The dog has reviewed the proposal and would like to leave the meeting."

Example weak lines:

- "OMG that was insane!"
- "Bro really said nope."
- "I can't believe this happened."
- "Wait until you see what happens next."
- "Like and subscribe if you agree."

The weak examples are generic enough to fit thousands of clips; therefore they do not build a recognizable host.

## Interaction rituals

### 1. Official Ruling

The host gives a compact mock verdict after the payoff. Viewers can overrule it.

Structure:

> observation → payoff → ruling → specific appeal question

Examples:

- "Official ruling: 8/10 recovery, minus two for causing the emergency. Fair?"
- "I have him guilty of unnecessary confidence. Appeal below."

### 2. Pick a Side

Use when the clip contains a real disagreement or choice.

Examples:

- "Roommate A or roommate B. No third-party diplomacy."
- "Does that count as cheating or creative interpretation?"

### 3. Prediction

Ask viewers to commit before a reveal when the source genuinely supports it.

Do not spoil the result in the opening frame or title.

### 4. Comment Callback

Turn a strong viewer comment, argument, correction, or running joke into a future Short. The callback should identify the real comment context, not fabricate audience participation.

### 5. The Scoreboard

A recurring lightweight scoring language can emerge around behaviors rather than people. Example dimensions:

- execution;
- confidence;
- decision-making;
- recovery;
- chaos created.

Do not display all dimensions on every clip. The joke dies if the UI becomes a mandatory rubric.

## Visual direction: `signal_v1`

The visual identity should feel **bold, clean, slightly irreverent, and mobile-native**.

### Launch palette

| Token | Hex | Role |
| --- | --- | --- |
| Ink | `#101216` | primary dark / shadow / end-card background |
| Paper | `#F6F3EC` | primary light text / warm neutral |
| Signal Blue | `#5B6CFF` | main brand accent / highlight |
| Hot Peach | `#FF7657` | secondary accent / punchline or warning |
| Volt | `#D9FF57` | rare emphasis / score or key word, <=10% of branded accents |

Usage rule: no frame should compete with all three accents at once. Signal Blue is the default brand anchor; Hot Peach and Volt are punctuation.

### Shape language

- medium corner radius rather than fully bubbly pills everywhere;
- strong blocks with slightly imperfect / offset motion when appropriate;
- thin technical lines only for mock-analysis jokes;
- no permanent HUD;
- no excessive gradients;
- no glowing neon borders around the entire frame.

### Typography direction

Desired personality:

- heavy grotesk / display sans for punch words;
- clean modern sans for small utility text;
- high legibility at phone size;
- slightly condensed display option is acceptable for scorecards / rulings.

The exact font family is intentionally not frozen in this first commit because it should be evaluated in rendered motion samples and must be safely distributable/embeddable in the renderer.

## Caption treatment: `impact_clean_v1`

Baseline caption rules:

- narration captions only;
- 2–5 spoken words per chunk when timing permits;
- max two lines;
- sentence case;
- heavy weight;
- Paper text with Ink outline/shadow;
- a single emphasis span may switch to Signal Blue, Hot Peach, or Volt based on semantic role;
- captions sit high enough to clear Shorts controls;
- move away from detected faces/source text when a future layout-aware renderer supports it;
- no continuous word-by-word bouncing baseline;
- punch words can receive one quick scale-in, not repeated jitter.

## Motion treatment: `restrained_punch_v1`

Motion should correspond to meaning.

Allowed launch moves:

- 102–108% punch-in on a reveal or facial reaction;
- 4–8 frame freeze for a mock ruling or hidden-detail highlight;
- quick label slide for an official ruling;
- short underline / bracket animation around a real detail;
- subtle caption emphasis scale;
- hard cut when the joke benefits from abruptness.

Avoid:

- random zoom every 1–2 seconds;
- constant camera shake;
- generic whoosh transitions between every beat;
- emoji explosions;
- repeated meme sound effects as a substitute for writing.

## End-card treatment: `verdict_v1`

Do not stop the Short for a generic CTA card.

If an interaction prompt exists, the preferred treatment is a **brief overlay integrated into the final visual beat**, not a long opaque slate. The source should remain visually present when practical.

The end treatment may include:

- official ruling;
- A/B choice;
- one short question;
- compact score;
- callback tag.

Target duration: approximately 0.6–1.5 seconds unless spoken narration requires longer.

## Branding mark

At launch, do not use a large permanent logo. Recognition should first come from voice, captions, accent treatment, and recurring formats.

A future channel mark may appear as a small corner bug only if tests show it does not clutter the frame.

## Naming

The channel name and host name are not frozen in this document.

A final name must be:

- easy to say aloud;
- easy to spell after hearing once;
- short enough to function as a watermark/handle;
- not locked to one clip category if Channel 01 remains broad entertainment;
- able to support the host personality without sounding like an AI product;
- legally/search-distinct enough to build recognition.

Do not launch publicly until this decision is frozen into Brand v1.

## Packaging

### Titles

Preferred title behavior:

- short enough to read quickly;
- specific enough to communicate the social/visual idea;
- no duplicated hook line unless repetition is useful;
- no fake stakes;
- no unnecessary hashtags inside the title;
- no generic "funniest video ever" language.

Example families:

- "He Had One Job. Then He Added Two More."
- "Who Is Actually Wrong Here?"
- "Watch the Second Attempt"
- "The Dog Has Made a Decision"
- "This Is Why You Read the Instructions"

These are structural examples, not reusable templates to paste mechanically.

### Thumbnail / cover

For long-form or other thumbnail surfaces:

- one focal face/object/action;
- 0–4 words;
- Signal Blue as the usual directional highlight;
- Hot Peach for conflict/error;
- Volt for a number/score/key detail;
- avoid multi-face collage unless the concept is explicitly versus/comparison;
- do not repeat title text exactly.

## Source-category adaptability

The personality remains stable even when the clip category changes.

- Comedy: more observation, less narration.
- Animals: do not infantilize; mock-analysis can work well.
- Feel-good: lower sarcasm; allow sincere warmth.
- Tech/AI: precise skepticism and explanations; never fake specs.
- Relationships/social: avoid cruelty and one-sided moral certainty from incomplete context.
- Food/travel: sensory specificity over generic amazement.
- Financial/mindset: trust and accuracy outrank punchline density.

The host should feel like one person with context-sensitive manners, not twelve category personas.

## Launch experiment discipline

Thirty Shorts can reveal directional patterns, but they are not enough to declare universal causal laws. The first 30 should deliberately generate comparable evidence.

Rules:

- one primary treatment change per test whenever possible;
- balance content categories across treatment cohorts;
- do not compare an obviously exceptional source clip against a weak source and attribute the result to captions;
- record all treatment identifiers;
- keep Brand v1 identity stable while testing presentation tactics;
- use retention curves and engagement quality, not just raw views.

## First 30-video matrix

### Videos 1–6 — baseline / hook families

Keep visual treatment, caption style, voice, and interaction behavior fixed. Rotate the opening treatment:

1. hidden detail
2. social verdict
3. precise curiosity
4. no host pre-hook (strong native source opening)
5. mock stakes
6. prediction

Primary question: which hook families create the strongest chose-to-view / early retention without harming completion?

### Videos 7–12 — interaction ritual

Use the two strongest hook families from 1–6. Keep visuals fixed. Rotate:

7. no interaction prompt
8. official ruling
9. pick a side
10. prediction + result
11. compact scorecard
12. specific open-ended judgment

Primary question: which rituals produce substantive comments/shares without hurting completion?

### Videos 13–18 — narration density

Hold hook/interaction families roughly constant. Test:

13–14. minimal: one host segment
15–16. standard: pre + post
17–18. high but purposeful: pre + one justified mid + post

Primary question: how much host presence strengthens the brand before it begins competing with the clip?

### Videos 19–24 — motion/caption intensity

Use similar source structures when possible.

19–20. baseline captions, minimal motion
21–22. semantic word emphasis + one punch-in
23–24. semantic emphasis + punch-in/freeze/label where earned

Primary question: does added visual emphasis improve retention/spikes, or simply add noise?

### Videos 25–30 — repeatability / callbacks

Use the strongest treatment combination so far and test whether recognizable continuity helps:

25. baseline winning treatment
26. official ruling callback
27. scorecard callback
28. real comment callback if available; otherwise baseline
29. repeated visual label / motif
30. strongest validated combination with no new variable

Primary question: are viewers beginning to recognize and participate in recurring channel language?

## Review checkpoints

After video 10:

- reject obviously weak hook/interaction patterns;
- do not rebrand.

After video 20:

- inspect source-adjusted retention and comment quality;
- identify whether host density is too high/low;
- inspect whether the voice still feels natural across categories.

After video 30:

- propose Brand v1.1 only if a repeated pattern has enough evidence to justify a versioned change;
- preserve the winning identity traits even if a visual treatment changes;
- decide whether the channel name / host name / logo mark should now be frozen based on the actual rendered personality.

## Launch blockers

Before first public publish, freeze:

- channel name / handle;
- host public name, or explicit decision that the host remains unnamed;
- final distributable font package;
- profile image / avatar direction;
- banner direction;
- Brand v1 database/config record;
- voice v2 provider samples and selected profile;
- renderer baseline treatment;
- made-for-kids default and synthetic-media disclosure behavior already supported by Katcha's publishing path.
