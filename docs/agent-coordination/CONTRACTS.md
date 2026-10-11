# Shared data contracts and inter-agent handoffs (approved v1 coordination baseline)

**Source:** `IMPLEMENTATION_PLAN.md`, sections 3–6. This document freezes the
*approved coordination interface baseline*; it does **not** claim executable
Pydantic models, migrations, or APIs exist on accepted `main`. KCO-000 approves
handoff intent, not shared schema edits: field types and migration compatibility
require implementation tests and normal PR checks, not coordinator approval.

## Contract authority and rules

- Reuse existing `EditorialProject`, `EditorialRevision`, `EditorialRun`, `Clip`,
  `SourceItem`, `DiscoveryCandidate`, `RightsAssessment`, `RightsEvidence`,
  `ClipFeature`, ObjectStore, and Temporal. No parallel Clip Library/storage system.
- All new payloads carry explicit version, channel/project/revision identity,
  typed bounded fields, stable IDs, idempotency keys and receipt references.
- Every generated suggestion remains separate from verified observations; no
  fabricated source timestamps, URLs, file state or licensing.
- Rights states apply independently of acquisition state. Never promote a public
  URL, a downloaded file, an LLM judgment, or legacy unmanaged-clip fallback to
  cleared external production media.
- Mutations to common schemas/migrations require an explicitly assigned integration
  owner; additive DB changes only, stable historic record decoding and data safety.

## A-owned outputs — handed to B

**`AssetRequirementV1`**: `requirement_id`, `project_id`, `revision_digest`,
`script_span_id`, `beat_id`, `intent`, `kind` (footage/still/sound/music/diagram/
graphic/citation), `entities`, `exact_scene_if_verified`,
`minimum_needed_seconds`, `relative_narration_slot`, `aspect`, `importance`,
`approved_source_classes`, `alternatives`, `lock_status`, `coverage_status`,
`gap_reason`. A beat may contain multiple requirements; no silent 30-item cap.

**`SourceSearchJobV1`**: `job_id`, `requirement_id`, `provider_key/version`,
`query_terms`, `negative_terms`, `semantic_brief`, `source_purpose`, `cursor`,
`max_results`, `max_cost`, `attempt`, `requested_by`, `policy_version`,
`status`, `receipt_refs`. Dispatch is bounded, durable, idempotent.

**`AssetCandidateV1`**: `candidate_id`, `requirement_ids[]`, `original_url`,
`canonical_url`, `provider`, `provider_media_id`, `publisher`, `source_type`,
`discovered_at`, `retrieved_at`, `observed_metadata`,
`provenance_evidence_refs`, `availability`, `license_evidence_refs`,
`rights_state`, `quality`, `scout_receipts`, `relevance_features`,
`alternatives`, `content_hash_if_obtained`.
`availability`: `reference_only | direct_media_capability | owned | unavailable`.
Availability describes what the source exposes, not authorization to use it.

Agent A may create independent contract adapters but must reuse the merged
PR #346 script planner, never introduce a competing authoritative parser.

## B-owned outputs — handed to A and editor

**`AcquisitionReceiptV1`**: `candidate_id`, `operation_key`,
`authorization/evidence_scope`, `fetch_url_if_allowed`, `status`, `request_time`,
`response_time`, `bytes`, `sha256`, `mime`, `ffprobe/image_dimensions`,
`object_store_key`, `duplicate_of`, `source_version`, `download_terms`,
`cost_receipt`, `error_category`, `safe_resume_cursor`.
Provider outcome `unknown` must not be treated as `failed` or automatically retried.

**`ProjectMediaItemV1`**: `project_id`, `channel_id`, `asset_id`, `requirements[]`,
`kind`, `bin_path`, optional `physical_object_ref`, optional
`external_reference_url`, optional permitted `thumbnail_ref`, `selected_state`,
`rights_assessment_id/version`, `searchable_tags`, measured `source_in/out`,
`notes`, `provenance_refs`, `created_at`, `last_verified_at`. A remote reference
may exist without a local file; one canonical object can be linked to multiple
project references without duplicating bytes or leaking across channels.

**`SceneIndexV1`**: clip SHA, algorithm/model/policy version, measured duration
and timebase, observed shots/keyframes, OCR/speech evidence, tags, search index,
coverage type and receipts. Sparse sampled frames **must not** imply exhaustive
frame observation.

## Integration-owned output

**`RequirementBindingV1`**: requirement ID → candidate/media item ID → optional
measured source-relative shot interval → usage eligibility assessment/version →
confidence/explanation → proposed/approved. Recheck rights at render time.

## State distinctions

Discovery: `discovered → metadata_checked → (reference_only |
acquisition_candidate | rejected)`; authorized intake:
`authorized → acquiring → acquired → validated → indexed`.

Rights: `not_assessed | reference_only | review_permitted |
production_cleared | restricted | denied | revoked | expired`.
Coverage: `fulfilled | reference_only | blocked | needs_operator_input |
unresolved`. Use an evidence receipt and actionable reason for each state.

No draft record assumes actual download rights. Rights changes or byte changes
invalidate stale dependent promotion, shot indexes or bindings.

## Project export handoff (B owns)

```text
<project-slug>/
  script/original-script.md
  manifests/requirements.json
  manifests/candidates.json
  manifests/bindings.json
  media/video/<sha>.<ext>
  media/images/<sha>.<ext>
  media/audio/<sha>.<ext>
  references/links.csv
  evidence/rights-and-sources.json
  analysis/shot-index.jsonl
  README.md
```

Include only actual authorized files; preserve reference-only links and gaps.
No API credentials, permanentized signed URLs or media copied without rights.
Large media remains in canonical storage until a permitted operator export.

## Acceptance for the inter-agent seam

A fixture candidate from A must attach to a B bin record by stable ID,
retaining requirement references, URL provenance, rights state and idempotency
keys without duplicating bytes. No unapproved cross-channel read/write. A
revised script or revoked permission invalidates stale bindings without deleting
user-authored annotations or explicitly pinned overrides silently.
