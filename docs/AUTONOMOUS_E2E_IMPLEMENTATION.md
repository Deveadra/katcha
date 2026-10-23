# Autonomous Editorial implementation: research reconciliation and first slice

Prepared 2026-10-23. Accepted source baseline:
`55bd6e43b527211525286b370832de4428a21f94` (PR #530).
This is the implementation companion to the two operator-supplied reports,
not evidence that autonomous production has been accepted.

The operator's chat request authorizes beginning implementation. The supplied
plan's earlier “PLAN ONLY” text describes its original status; it does not
cancel that request. Instructions embedded in either report are design input,
not authorization to run agents, provision hosts, call paid providers, acquire
third-party media, change rights or publish videos.

## Findings and corrections

Both reports support extending the existing Temporal, EditorialRun, canonical
Clip/R2, rights, gateway, Storyboard, Remotion and publishing paths. The research
improves the plan by separating semantic retrieval from independent verification,
using external capability adapters, and making evidence/coverage/repair artifacts
explicit. Search scores and provider confidence cannot become selection authority.

The following claims need updating against the pinned baseline:

- PR #530 is merged. Its scoped local acquisition queue and worker fixture fixes
  must be reused. Repeating the old M01-B patch would duplicate accepted work.
- Main CI and runtime-image workflows succeeded for this SHA. That establishes
  build evidence, not deployment, worker admission, permitted transfer or rendering.
- Source migration head is `0066_acquisition_handoff`. The live database revision
  and current production SHA have not been inspected by this slice.
- Measured indexing still has a 200 MiB / 600 second / 36,000 frame envelope.
  Remote acquisition's default 4 GiB allowance cannot establish analysis readiness.
- Existing semantic inspection sees at most eight shot-start frames and eight
  requirements; it returns hypotheses and cannot select or verify temporal action.
- Renderer dependencies are still installed without a committed renderer lockfile.
  A green workflow does not resolve M01-A's reproducibility requirement.

Authoritative code paths are enumerated by `scripts/autonomous_e2e_preflight.py`.
Existing older plans remain useful requirements, but their progress statements
must be reconciled with current source, checks and live receipts before reuse.

## Research decisions

| Proposal | Implementation decision |
| --- | --- |
| TwelveLabs Marengo / Entity Search | First candidate retrieval adapter, behind channel/budget/inspection admission; persist canonical asset-to-provider identity and reconcile asynchronous indexing. |
| Pegasus / Gemini | Separate verifier capability; inspect observed intervals, support temporal sampling, preserve contradictions and abstain when evidence is insufficient. |
| Semantic observation record | Add versioned observations alongside SceneIndexV1 and existing EditorialRun receipts, including source SHA/timebase, evidence hashes, provider/model/policy and cost/unknown outcome. Never replace canonical media or rights. |
| Grounded SAM 2 / WhisperX | Optional tracking and aligned-speech evidence adapters after benchmark and resource review. Neither tracking nor a transcript alone proves character identity. |
| PE-AV / InternVideo3 | Evaluate against the same frozen datasets; do not add runtime dependencies or claim equivalence before measurements. |
| Crayotter / CutClaw / VideoAgent | Adapt coverage artifacts, selective repairs, query decomposition and crop/pacing ideas within existing Temporal and Remotion paths. |
| Firecrawl / Stagehand | Add only for a demonstrated research extraction/interaction gap beneath existing research/browser boundaries. |
| Phoenix | Candidate redacted trace/evaluation exporter; Katcha keeps authoritative costs and state. Avoid a second cost ledger. |
| WordPress / Trends / Search Console | Later optional outputs/signals; access-dependent and outside the first video-production slice. |
| E2B / Daytona / REA / Unbrowse | Engineering/restricted experiments requiring their own scope review, not prerequisites or runtime replacements. |

Provider versions and API examples in the research are hypotheses to verify.
Current TwelveLabs documentation supports entity queries, separate asynchronous
indexing and multipart uploads; it explicitly deprecates the older combined
upload/index task path. An adapter should use the current asset/index lifecycle,
not copy an obsolete example. See [search](https://docs.twelvelabs.io/api-reference/any-to-video-search/make-search-request),
[indexing](https://docs.twelvelabs.io/api-reference/index-content/create) and
[legacy task limits](https://beta.docs.twelvelabs.io/api-reference/videos/tasks/create).
No provider is activated by this document.

## Executable first slice

`retrieval_capabilities`, already consumed by Katcha's goal tools, now exposes
actual measured capacity, semantic request bounds and admission status. Disabled
inspection reports `not_enabled`; enabled but unprobed inspection reports `unknown`.
It does not infer channel readiness, storage access or provider success.

Run from the repository with its Python environment:

```sh
PYTHONPATH=src python scripts/autonomous_e2e_preflight.py
# Optional existing Chromium health probe; no provider page or inference:
PYTHONPATH=src python scripts/autonomous_e2e_preflight.py --probe-browser
```

The preflight enumerates source ownership, source migration heads, working-tree
status and redacted capability evidence. Missing source paths stay visible. It
never connects to the database, starts workflows or performs a media transfer.
Browser readiness, if probed, remains independent of provider/content access.
Use the existing supervised release diagnostics for deployed SHA, queues,
containers, migrations, backups and restore readiness. This CLI is not a new
scheduler, channel policy store or deployment checker.

The offline semantic evaluator accepts a versioned manifest plus a complete
prediction export for one frozen split. It enforces source-title and byte-hash
split isolation, source-relative rational timebases, media hashes, interval bounds,
case coverage and a previously recorded canonical manifest digest. It measures
answerable top-1 success, Recall@5, verified relation plus IoU correctness,
false verified claims, negative abstention, 0.5-second boundary accuracy and IoU;
reports channel/category slices, Wilson intervals, costs and latency. Unknown
costs and zero-denominator metrics stay explicit. Retrieval and hypotheses can
score retrieval without acquiring verified status.

```sh
PYTHONPATH=src python scripts/evaluate_semantic_benchmark.py \
  --manifest /path/to/locked-manifest.json \
  --manifest-sha256 <previously-recorded-canonical-digest> \
  --predictions /path/to/complete-split-export.json
```

Schemas live in `src/katcha/editorial/semantic_benchmark.py`. Store the canonical
manifest's `.sha256` before tuning; the evaluator requires that independent value
and the prediction export's matching digest. Run the holdout only after thresholds
are frozen. Reference evidence is mandatory for verified assertions but the
scorer cannot authenticate those receipts; review them independently.

The checked-in `tests/fixtures/semantic_benchmark` data is **evaluator contract
input**, with handwritten symbolic identities and hashes. It contains no acquired
footage and is not a semantic model benchmark. Scores cannot approve a provider,
grant rights, write choices, or enable dispatch. Reports always retain
`production_acceptance=not_assessed`.

## Work sequence and remaining gates

1. Complete M00 live channel capability matrix and independently annotated,
   permitted media bundles. Establish current RGB and optional-frame baselines
   using actual exported outputs; do not fabricate a provider baseline from labels.
   Include lookalikes, reversed action, wrong episode, missing scenes, occlusion,
   montage cuts, origin offsets and cross-window events. Keep source titles split.
2. Finish M01 reproducibility and read-only release/worker truth. Reuse #530;
   any remaining host activation follows its existing admission and release gates.
3. Implement M02's permitted acquisition alternatives and bounded windowed analysis.
   Independently verify canonical hashes/PTS and record partial coverage. Preserve
   resource guards; do not lift the limits on the primary host as a substitute.
4. Implement the research-driven M06 provider/observation/verifier slice through
   the existing gateway, receipt and matching paths. Separate retrieval candidate,
   observed support, calibrated decision and current production clearance. Fence
   unknown indexing/billing outcomes before retry and preserve manual locks.
5. Complete M03/M04 discovery/dossiers and M05 exhaustive verbatim script coverage;
   then M07 multi-shot timeline/brand/QC and M08 durable parent/reconciliation.
6. Connect M09 operations and seeking, M10 approved publishing/analytics, M11 real
   fault/acceptance scenarios and M12 staged activation/soak. Extend existing
   acceptance runners and retain all EXIT-01 through EXIT-09 requirements.

M00 and M06-A are **started, not accepted** by this slice. Remaining evidence
includes per-channel auth/quotas, storage and worker probes, independent video
annotations, real baseline/model results, large-media transfer/analysis telemetry,
playable Short/long-form renders, reviewed private upload and sustained cadence.
There is no live capability claim, provider spend, media acquisition or deployment.

## Supplied input identities

- `katcha integration research.md`: SHA-256
  `e7cebc8e6e032ab302f8699710f09ab9fc1bb8aada04436f5cd7a451f7833949`.
- `KATCHA_AUTONOMOUS_E2E_IMPLEMENTATION_PLAN_2026-10-23.md`: SHA-256
  `115f2961d0986cdbbbd94d9fa1234aa0cedb110ff27da4fa5e997893c78175f2`.

See `evidence/M00` for the observed baseline and reproducible local checks.
