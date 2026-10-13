# Approved measured shots in Editorial Storyboard (KIN-102)

In **Editorial → Assets**, index permitted acquired footage, then use **Choose
measured shots for Storyboard**. Choose up to eight footage references and select
**Propose measured shots**. This consumes KSI-106's existing scoped matching API;
it does not call providers, acquire files, decode again or change matching logic.

Review each interval's explanation, observed tags, confidence limits, permission
state and unresolved gaps. **Save shot selection** records your own locked choice
and note through the existing append-only choice API. Color/brightness matches
and requirement-linked fallbacks do not establish characters, actions, exact
scenes or comprehensive observation. A review selection cannot grant rights.

Choose a target saved script beat and **Add selected shot to Storyboard**. Only
current, explicitly selected choices with current production permission can enter
this handoff. Source assignment is an explicit editorial decision, not a semantic
coverage assertion. An imported script without saved Storyboard beats needs saved
script beats first. Unsaved script edits must be saved before handoff.

Pending Storyboard edits are saved first. The handoff defaults to protecting an
existing visual; **Replace this beat's current visual** permits that particular
replacement and retains the prior workspace in Undo history. Other beats,
caption treatments and narration selections remain intact. Source-specific edits
on the explicitly replaced beat remain in its historical workspace. Replays use
the same saved request identity and expected workspace version after an uncertain
response. A concurrent edit or mismatched replay fails without overwriting it.

For playback, the complete beat must fit inside the approved measured interval.
An explicit **Hold the measured keyframe** choice holds an observed frame through
the beat instead. Later timing, speed or start edits are permitted only inside
that measured interval. Private preview rejects spill into the next shot; shorten
the beat or deliberately hold a measured frame. Original source origin is
provenance and is never added to renderer source-relative offsets.

## Existing pipeline and authority

`POST /v1/channels/{channel}/editorial-projects/{project}/storyboard/approved-shot`
accepts only choice ID, saved script revision, expected Storyboard version, target
beat, idempotency key and explicit replacement/freeze flags. It uses existing
channel write authorization and existing versioned Storyboard saves/history.
Clients cannot submit file keys, rights flags or fabricated measured evidence.

An `approved_shot_<immutable-choice-UUID-hex>` media identity references the saved
choice in the existing VisualMediaUse contract. Canonical Clip/storage identities
and RenderMedia are resolved server-side. Existing acquisition-backed footage,
quote/still/narration selections, Direction receipts and renderer versions retain
their existing contracts. No media copies, synthetic acquisition runs, duplicate
indexes or migrations are introduced.

The adapter reuses KSI-106's existing read-only index/source snapshot and its
unchanged dependency digest over the immutable proposal package, preparation,
scoped Media Bin, source bytes and policy. It checks the preserved script digest,
latest operator choice, current production assessment/version, canonical hash and
current measured intervals. This avoids taking the proposal reader's additional
project write lock inside Storyboard/review transactions. No parser, ranking or
matching behavior is changed.

A compile/handoff verifies at most 30 shot choices, eight proposal packages, eight
footage references and the existing 512 MiB unique-source budget. Canonical bytes
are verified once over the combined set before metadata-only per-package checks.
Save and compile recheck approval/rights; rendering verifies again after its durable
dispatch claim, immediately before the renderer/recovery call, and before output
promotion. A new request rejected before calling the renderer can safely resume;
a previously uncertain dispatch retains its uncertainty. Preview access, review and publication continue to
use current manifest verification. Revocation, replacement, script edits, changed
bytes/index/Bin evidence or invalid timing blocks use and preserves history.

## Validation limits

Tests use generated three-color footage with a nonzero source timestamp origin,
isolated fixture databases and synthetic browser/provider responses. Rendering is
mocked for the final rights-revocation test. These checks demonstrate deterministic
scope, evidence, recovery and pipeline compatibility; they do not demonstrate live
provider acquisition, semantic understanding, paid render execution, deployment
or production rights for real media.
