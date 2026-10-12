# Script media preparation

In Production → Editorial → Assets, **Find & prepare media** saves visual
requirements for the project's imported script or its current saved script
beats. It then checks a bounded set of source pages and links ranked references
into the existing project Media Bin. Script edits must be saved first. The
original import, current draft, operator notes, selected clips and rights records
remain intact.

Public-provider metadata search is opt-in. An unchecked permission saves the
requirements, existing owned-clip bindings and explicit search gaps without
external calls. Continue uses the permission saved with that preparation;
changing it requires completing the old preparation first. The installed
YouTube and Reddit discovery adapters are reused. No paid scout, downloader,
new parser, media store, automatic license grant or production action is added.

References retain provider-reported metadata, candidate IDs, observation/search
receipt IDs, requirement links and the exact script digest. Publisher authority,
scene match, source timings and rights remain unverified. Reference coverage
reports **zero usable media**. Already owned project clips are linked by canonical
Clip ID with unassessed rights; their bytes are not copied. An existing matching
Bin row is reused with its operator annotations intact, while the new preparation
stores additional requirement/evidence bindings. Existing permission-gated intake,
validation, export and render checks continue to govern actual files and use.

## API and persistence

All endpoints use the existing Editorial channel authorization, beneath
`/v1/channels/{channel_profile_id}/editorial-projects/{project_id}/media-preparation`:

- `POST ""`: `{idempotency_key, expected_revision, external_search_authorized}`
  saves an offline plan. A revision-zero imported project gets its first snapshot
  through the existing draft service, without generating or rewriting its script.
- `GET ""`: latest saved preparation or `null`; it starts no work.
- `GET /{run_id}`: scoped status, complete requirements/coverage, planning gaps,
  progress, receipts, Bin items and durable bindings. Old revisions remain readable
  and are labeled stale.
- `POST /{run_id}/resume`: performs at most five saved provider pages. The total
  plan has at most 48 jobs, with 24 calls/240 reserved results per provider,
  ten results per page and a zero monetary ceiling. Remaining requirements/pages
  have visible budget gaps. No automatic provider rejection/unknown retry occurs.
- `POST /{run_id}/reconcile`: `{confirmed_worker_stopped: true}` records unknown
  outcomes for stopped searches after their ten-minute recovery window. It never
  releases reservations or repeats a provider call. Refresh/Continue can instead
  observe a previously running search that has finished.

Existing `EditorialRun` stores version-one snapshots and fenced checkpoints;
`DomainEvent` records scoped progress. Preparation has separate statuses and
controls so it cannot enter legacy Temporal research/acquisition workflows or
replace the latest Editorial production run. Same-revision permission variants
are serialized by the existing project lock. Bin inserts reuse existing
idempotency and canonical references.

The authoritative `SearchJobLedger` lives at
`KATCHA_WORK_DIR/source-search-ledger.sqlite`. Preserve that file, its WAL and
the PostgreSQL project data across restarts. All API processes handling a saved
preparation must use the same persistent local evidence volume. SQLite WAL is
not a network-filesystem or independently replicated multi-pod ledger. Its durable
volume identity is pinned in each preparation; a missing/replaced volume fails
closed rather than issuing fresh searches. Restore its evidence before recovery.
Do not delete evidence as rollback cleanup.

Saved beat envelopes exceeding the accepted parser's 120,000-character limit
are rejected with an explicit action; the saved script remains untouched. The
integration does not fabricate partial coverage by truncating the script.

## Offline proof

`tests/test_script_to_media_bin.py` exercises the actual SQLite models/services,
the durable search ledger and FastAPI routes using trusted fixture adapters.
It blocks network transport. The full-script case has more than 30 visual
requirements, a duplicate source, pagination, provider failure, a canonical owned
Clip, preserved restricted operator selection, exact script provenance and real
coverage/budget gaps. Additional cases cover receipt/Bin crash checkpoints,
reopened ledger replay, channel scope, stale/concurrent edits, lost evidence,
leased work, explicit stopped-worker reconciliation, paid/authorization guards,
publication deduplication and historical Editorial compatibility.

Run `pytest -q tests/test_script_to_media_bin.py` and the existing search/Bin
regression suites. `web-tests/editorial.cjs` verifies the connected UI journey,
response-loss recovery, rights/gaps, unsaved edits, explicit reconciliation,
keyboard use, 390px layout and late channel responses. These are offline fixture
proofs, not live provider, permission, acquired-file or production evidence.
