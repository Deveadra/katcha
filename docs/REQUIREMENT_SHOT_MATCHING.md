# Requirement-to-shot proposals (KSI-106)

KSI-106 connects KIN-101's saved `AssetManifestV1` and Media Bin requirement links
to KMI-107's measured `SceneIndexV1`. It adds backend APIs and additive durable
proposal/choice tables. It does not change the indexing UI, storyboard, compiler,
renderer, source media, Bin selections, annotations, rights, or historic renders.

## Evidence and confidence

Every manifest requirement appears exactly once, including narration placeholders
and non-video requirements. Exact visual-token cues (`red`, `green`, `blue`,
`dark`, `bright`, `mid-tone`, including combinations) may match tags on a shot's
first observed frame. Longer semantic cues, including `red car` and `bright car
chase`, do not match colors as evidence of objects, actions, characters or scenes.

An explicit current requirement link from the Bin or preparation may propose
measured intervals as source-review fallbacks. This relation proves that somebody
linked a source, not that its footage depicts the requested scene. Such proposals
retain `semantic_intent_unobserved` and `unverified_semantics`. Audio, stills,
diagrams, graphics, citations, OCR and speech remain outside this video evidence.
There is no automatic fulfilled-coverage claim or semantic approval.

Ranking score = 2 for an exact measured visual-token match + 1 for a current
requirement link. Ties sort by item ID, index ID, measured start tick and shot ID.
Scores are evidence priorities, not probabilities. Each binding includes its
explanation, confidence limits, Clip SHA, item/index/shot IDs, start/end/keyframe
ticks, timebase, source origin, duration, source-relative seconds, keyframe hash,
coverage/policy, and stable preparation, Bin, shot and permission evidence refs. Owned sources
retain a deterministic representative upload reference; the dependency digest
covers the complete current permission set.
`start <= keyframe < end <= duration`; normalized seconds must agree with ticks.
The source origin is retained for provenance and is never added to relative times.
Whole measured intervals must also fit any operator-supplied Bin in/out bounds;
the matcher does not fabricate partial-shot timestamps.

## Scope, integrity and rights

Preparation must belong to the channel/project and exact current saved revision.
Its preserved-script digest must match. Scope the entire selected Bin set before
reading any bytes. Older Bin references can be intentionally reused by KIN-101;
the matcher uses current requirement identities, scoped canonical media, and
current permission, rather than treating an old reference as proof of relevance.

The existing scoped index service verifies actual source SHA/size and latest
inspection authority before using saved measurements. Every request and saved
package read rechecks current evidence without decoding or calling providers.
Missing/unfinished indexes and blocked inspection produce explicit per-item gaps.
Corrupted bytes and changes during verification fail closed. Permission, Bin,
index or script changes invalidate package reuse; the original evidence stays
available for audit and choices are never deleted. A stale revision returns audit
proposals marked stale without reading source bytes or claiming eligibility.

Inspection and production eligibility differ. Owned uploads can be inspected
while production rights remain `not_assessed`. Eligibility requires a currently
`production_cleared` Bin item and the index service's matching latest eligible
assessment/validated acquisition. Proposals and operator choices always carry
`rights_granted=false`. Stale returned bindings, including nested operator choice
bindings, have `production_eligible=false`. This is a checked snapshot, not a
permanent render authorization: KIN-102 must recheck at compile/render time.

## Bounds and durable choices

- Up to 12,000 requirements, eight selected items and 512 MiB unique source bytes.
- At most five proposals per requirement and 4,096 per package. Every omitted
  requirement/candidate remains visible through explicit budget/review gaps.
- Evidence digests are computed once per selected index; full binding objects are
  constructed only for shortlisted candidates. Exhausted-budget requirements do
  not materialize further shot candidates.
- Provider calls, new decoding and media copies: zero.

Proposal packages are immutable and scoped by project/channel/request identity.
Exact request replay revalidates bytes and dependencies and returns the saved
package. Changed options, links, notes, permission, source SHA/index or script
require a new request identity. Receipts retain the initiating actor/time.

Operator choice history is append-only and separate from recomputation. Selection
can use only a currently validated package binding. An explicit replacement must
supply the latest choice version; concurrent replacements fail with a conflict.
Locks, notes, prior measured selections and old revisions remain in history. A
package read returns the latest choice for each requirement in that package;
choices originating in another package are marked `requires_revalidation` and
are not production eligible. The original package remains readable for removed
requirements. Replaying a choice request does not append a second decision.

## Backend API / KIN-102 handoff

Under `/v1/channels/{channel}/editorial-projects/{project}/shot-proposals`:

- `POST ""`: `{preparation_run_id, expected_revision, item_ids, idempotency_key}`.
- `GET /{package_id}`: saved package plus `current_status` and `operator_choices`.
- `POST /choices`: `{package_id, binding_id, expected_revision,
  expected_choice_version, idempotency_key, locked:true, note:"..."}`.

Existing channel authentication applies. Matching and explicit choices require
write scope; reading requires read scope. Caller timestamps, evidence, arbitrary
source URLs/storage keys, rights flags, budgets and confidence are not accepted.

`RequirementBindingV1` is the typed binding contract in
`editorial/source_intelligence/shot_contracts.py`. Response package status and
operator choice envelopes are separate from the immutable package contract.
Agent B should consume saved IDs and source-relative intervals, keep gap/confidence
limits visible, preserve manual choices and existing Editorial schemas, and call
current scoped revalidation before binding/compile/render. A source-review fallback
must never become an exact-scene assertion through selection alone. The indexing
UI and Editorial direction/storyboard integration remain Agent B's KIN-102 work.

Acceptance uses locally generated three-color footage with a nonzero source
origin and 0.1-second frame tolerance, real script/Bin/index services and isolated
fixture databases. These tests prove deterministic evidence and invalidation,
not real-provider semantic understanding or production clearance.
