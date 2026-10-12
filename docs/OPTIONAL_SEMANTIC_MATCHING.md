# Optional semantic shot suggestions

KSI-106's existing requirement matching ranks measured RGB/color tokens or
operator requirement links. SceneIndexV1 measures source-relative intervals,
shot-start presentation timestamps and pixel hashes. A red frame proves a
measured color descriptor at that frame. It does not establish a character,
action, scene, object identity, motion or the same appearance throughout the
interval. The existing path remains provider-free and unchanged.

Optional semantic matching adds a separate backend inspection of actual visible
pixels. It asks the existing Editorial provider gateway to propose uncertain
requirement-to-frame matches. These are **AI hypotheses, never verified facts**.
The provider may misidentify objects, characters or scenes. Sparse start frames
cannot prove motion, dialogue, intervening actions, exact scene context or
exhaustive coverage. Unsupported matches stay gaps; low/medium confidence is an
uncalibrated model judgment. Human review is always required and never grants
footage rights. The service cannot select footage, create approved bindings,
fulfill coverage, render or alter operator choices.

This capability is separate from KQA-101's existing pipeline acceptance. No
indexing UI, live provider validation or deployment is included.

## Explicit prerequisites and enforced bounds

- Off by default: every POST requires `external_inspection_authorized=true` from
  an authenticated channel-scoped writer. This authorizes transmitting the
  selected frame pixels and selected requirement text to the configured
  provider; ordinary measured matching/indexing never invokes this capability.
- A current KIN-101 preparation for the exact project/script revision and one
  acquired canonical video with current SceneIndexV1 are required. Requirement
  media-kind and approved-source-class policy must match the chosen source.
- Current inspection permission, source identity, canonical byte size/SHA and
  complete measured index are checked before extraction, immediately before
  transmission, after provider completion, and on every read/replay. Revoked,
  restricted, expired, reference-only, missing, corrupt or stale evidence fails
  closed. Permission changes during a submitted call cannot erase that call;
  its receipt remains but no result is published or returned as current.
- At most eight distinct requirements, eight distinct measured shot-start
  frames, one source and one provider submission per inspection. Context JSON
  is at most 16,000 characters. A request is a bounded slice; requirements
  outside the slice are explicitly unexamined rather than silently fulfilled.
- Existing source limits apply: 200 MiB, 600 seconds, 36,000 decoded frames,
  16,777,216 pixels per frame. The isolated decoder retains 60 CPU seconds,
  90 wall seconds, 1.5 GiB memory and one decoder slot per API process. Each
  chosen frame's exact timebase/timestamp and 64x36 pixel SHA must match the
  saved measured evidence. The model receives a 256x144-pixel version of each
  selected original frame, in a contact sheet of at most 1024x288 pixels/2 MiB.
- Gateway reservation budget: 300,000 conservative tokens, one call, a
  300-second admission window and an enforced 2,048 output-token cap. At most
  sixteen hypotheses are accepted. Inputs/images are not saved as media blobs;
  the sheet SHA and provider receipt are durable. Temporary files are removed.
- Uses **existing configured free-tier Gemini only**. Existing live AI and
  channel routing policy gates remain authoritative. No paid fallback or new
  key infrastructure is introduced. Codex's current subscription adapter does
  not expose a verified hard output-token cap, so Codex-only configuration fails
  before provider submission. Requests do not change provider configuration.

## Backend contract

Under `/v1/channels/{channel}/editorial-projects/{project}/semantic-matches`:

- `POST /` takes `preparation_run_id`, `expected_revision`, `media_item_id`,
  `requirement_ids`, `shot_ids`, `idempotency_key`, and the explicit authorization
  above. The handler performs synchronous bounded inspection. Preserve request
  identity after an uncertain HTTP response.
- `GET /{run_id}` rechecks the current source, scope, permission and script before
  returning saved state. It never submits a call or decodes another frame.

Completed results use `SemanticShotSuggestionsV1`. Stable hypothesis IDs retain
canonical Clip SHA, channel/project/revision/preparation identity, index ID and
digest, shot ID, measured ticks/timebase/origin/in/out, measured frame-pixel SHA,
current permission references, provider call digest, model, token usage, image
SHA and output cap. Model output accepts only the selected requirement/shot IDs;
unknown IDs, duplicate pairs, supplied timestamps, extra fields, high confidence
or missing uncertainty fail validation. All coordinates come from the server's
measured index, never from the model. Ranking uses low/medium model confidence
then stable shot identity, without implying probabilities or verification.

Every selected requirement retains `semantic_hypothesis_requires_human_review`
and `current_rights_not_granted`. Missing model matches add
`no_supported_match_in_selected_frames`. Returned suggestions always carry
`verified_fact=false`, `human_review_required=true`, `rights_granted=false` and
`production_eligible=false`. Existing production rights/binding/render gates
remain separate.

## Crash, uncertainty and replay procedure

Inspection intent and provider receipts live in existing PostgreSQL
`editorial_runs`; no parallel SQLite ledger or migration is introduced. Preserve
these records with the existing PostgreSQL backup/restore process. Canonical
media must also be available: database-only restore cannot make missing bytes
inspectable. Restore/read without matching current bytes fails closed.

1. After a disconnected response or restart, GET the saved run or repeat the
   exact POST identity. Completed work reuses its saved receipt, after current
   evidence verification; no model submission or re-extraction occurs.
2. Running or blocked work exposes saved status and receipts with
   `automatic_retry_allowed=false`. A durable `started` receipt means the
   provider outcome is uncertain, including a process crash after receipt
   publication or after provider completion but before final checkpoint. Do
   not clear receipts, advance attempts or resubmit. Preserve the evidence and
   reconcile the outcome separately with the provider/operator.
3. Changing only the request key or input ordering cannot repeat a submitted
   inspection for the same evidence. Quota-rejected calls also require separate
   reconciliation; generic Editorial resume cannot retry this capability.
   Generic cancel may stop promotion while preserving all receipts.
4. A blocked/cancelled run with **no provider-call receipt** proves it stopped
   before submission. After fixing the local/configuration failure, an operator
   may explicitly authorize a new request identity. Replaying the old identity
   still returns its old state. This never retries an ambiguous call.
5. The ordinary Editorial reconciler never dispatches semantic runs, including
   interrupted runs. A partial/historical restore cannot prove the absence of
   later calls: retain production recovery/provider gates until the backup
   completeness and external-call ambiguity are reconciled. Restoring a backup
   is not authorization for a fresh call. No live recovery gates are changed by
   this feature.

## Offline proof and limits

`PYTHONPATH=src pytest -q tests/test_optional_semantic_matching.py` uses generated
three-color footage with a nonzero source origin and a fake provider response,
while exercising the real Editorial durable gateway. Tests cover actual frame
pixels/hash mismatch, scope, strict output IDs/timestamps/confidence, preserved
operator input, rights/source/script changes during transmission, restart and
uncertain replay, concurrent workers, unsupported provider caps, dispatch and
authorization. They prove safety contracts, not real semantic recognition or
provider accuracy. Live provider calls and real-media semantic acceptance require
separate authorization and operator review.
