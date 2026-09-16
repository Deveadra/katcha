# RankSnaxx — Katcha Channel 01

Status: launch operating contract

RankSnaxx is the first production channel operated by Katcha. It is not a separate product or a parallel automation stack. Katcha owns the shared capabilities — discovery, trend intelligence, provenance, rights qualification, media analysis, editorial planning, production, publishing, analytics, learning, budgeting, and eventual Aerith control. RankSnaxx supplies a channel-scoped identity, editorial format, host/voice direction, visual treatment, packaging grammar, and performance history.

The architectural rule is:

> **Katcha decides and executes how the system works. RankSnaxx tells Katcha what this channel is.**

Future channels should reuse the same Katcha services with their own versioned operating contracts rather than forking the pipeline.

## Channel identity

- Name: **RankSnaxx**
- YouTube handle: **@ranksnaxx**
- Surface: YouTube, Shorts-first at launch
- Audience: teens and young adults with broad entertainment appeal
- Host: funny, energetic, down-to-earth youth host defined by `youth_host` v2
- Core promise: **the internet is chaotic; RankSnaxx puts it in order**

The former `Channel 01` identifier remains a historical compatibility identity so previous production lineage can still be reproduced. New RankSnaxx profiles resolve to the named `ranksnaxx` brand contract.

## Editorial product

RankSnaxx is a **curated countdown entertainment channel**, not merely a rating channel.

The default viewing unit is:

> **one coherent premise + multiple strong clips + deliberate ordering + original host commentary**

A video may rank the clips explicitly, or the countdown may simply provide progression. Examples include:

- five funniest recoveries;
- five dogs that immediately regretted a decision;
- five trick shots that keep getting harder;
- five increasingly chaotic attempts at the same challenge;
- five inventions that look ridiculous but work;
- five clips where confidence exceeded preparation.

A list is not valid merely because five clips exist. They need a coherent premise and enough editorial connection that the viewer understands why they belong in the same episode.

## Versioned format

The launch format is `ranksnaxx_countdown` v1.0.0.

Default item count: **5**.

Supported launch variants: **3, 5, or 7** items. The format is intentionally bounded so Katcha can learn from comparable structures rather than producing arbitrary list lengths.

The default five-item structure is:

1. **#5 — opener:** instantly understandable and strong enough to stop the scroll;
2. **#4 — build:** establishes that the concept has depth beyond one good clip;
3. **#3 — escalation:** raises the expected payoff;
4. **#2 — false peak:** credible enough that it could have been number one;
5. **#1 — payoff:** strongest ending and most satisfying reason to finish the countdown.

Katcha assigns these positions deterministically from stored clip signals. The launch dimensions are:

- hook strength;
- visual clarity;
- payoff strength;
- escalation value;
- commentary opportunity;
- novelty;
- source quality;
- rights readiness.

Rights readiness is deliberately **not** mixed into the popularity/editorial score. It is an eligibility gate. A clip that looks viral but is not qualified for production does not enter a RankSnaxx plan.

## Commentary is the product layer

The source clip earns attention; RankSnaxx earns identity and transformation.

The host should add at least one meaningful layer: a joke, observation, frame, verdict, comparison, prediction, callback, or transition. Passive social-media compilation is not a supported RankSnaxx production mode.

Useful transition behavior includes:

- reacting to the previous payoff before revealing the next number;
- setting a narrow expectation for the next clip;
- using mock scoring or an "official ruling" when the source naturally supports it;
- creating a callback that pays off later in the list;
- disagreeing with an obvious ranking interpretation in a way viewers can debate.

The host should not narrate visuals the audience can already understand, manufacture stakes, force slang, or interrupt the strongest native moment merely to keep voice-over density high.

## Discovery and trend-intelligence relationship

For RankSnaxx, trend discovery should ultimately answer a different question than a news channel would.

It is not enough to detect that a topic is growing. Katcha should determine whether an emerging phenomenon contains enough coherent, qualified media to make a strong countdown **now**.

A future Phase 6.2 RankSnaxx opportunity packet should therefore include:

- canonical trend/topic cluster;
- growth velocity and acceleration;
- cross-source corroboration;
- freshness and saturation;
- number of candidate clips discovered;
- number of rights-ready candidates;
- number of candidates above the RankSnaxx editorial-quality floor;
- diversity of moments rather than five near-duplicates;
- estimated opener quality;
- estimated #1 payoff quality;
- commentary opportunity;
- proposed premise/title angle;
- evidence and provenance for every candidate.

A trend with enormous engagement but only one usable clip is not yet a RankSnaxx episode opportunity. A smaller trend with six highly legible, escalating, rights-ready clips may be much more actionable.

## Learning loop

RankSnaxx is also Katcha's first high-frequency experimental environment. Every published episode should eventually connect the format decisions made before production to actual performance.

Important treatment dimensions include:

- premise family;
- content category;
- item count;
- opener role score;
- #1 payoff score;
- ordering treatment;
- host treatment;
- narration density;
- interaction ritual;
- caption treatment;
- title/packaging family;
- source mix;
- trend stage at production time;
- lead time between trend detection and publication.

Outcome measurements should emphasize qualified viewing and durable channel growth: retention, completion, rewatching, shares, comments, returning viewers, subscriber conversion, and revenue when available. Raw starts alone are not a sufficient success signal.

Katcha should learn channel-specific correlations from these outcomes, but deterministic safety, provenance, rights, and originality gates remain outside learned optimization.

## Compatibility

`channel_01` remains the safe generic/legacy brand contract because historical productions may reference it. `RankSnaxx` is a named operating profile layered onto the same Katcha channel-brand system. This prevents the first channel from becoming hard-coded into the platform while also avoiding the opposite mistake: treating the real launch channel as an anonymous test fixture forever.
