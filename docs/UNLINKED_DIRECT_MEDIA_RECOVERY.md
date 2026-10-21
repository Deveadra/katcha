# Unlinked direct-media R2 recovery with durable source provenance

Before this change, the direct-media ingest could verify an object, copy it
from `staging/direct/<source-id>/<attempt>.<ext>` to content-addressed `raw/`,
then crash before committing a Clip row. A raw object named after its SHA-256
proves its content, **not its publisher or SourceItem ownership**.

## New pre-promotion checkpoint

Following verified stage SHA-256 and video-stream inspection, but **before**
moving any bytes into `raw/`, Katcha commits:

- `SourceItem.source_metadata.direct_promotion_proof`: exact SourceItem,
  persisted Temporal workflow ID, hash of canonical source URL (never the
  URL/query itself), stage attempt key, content-addressed raw key, SHA-256,
  length, extension and verified video inspection metadata
- A durable `source.direct_media_stage_verified` DomainEvent containing
  the SHA, storage key, size and fingerprint of the complete proof

A later attempt cannot overwrite a different unresolved proof. Ordinary
successful ingestion retains the provenance checkpoint and continues the
existing Clip deduplication and storage process. No database schema migration
is required.

**Previously orphaned raw files without a checkpoint and matching internal
audit event cannot be attributed by this recovery mechanism.**

## Operator inspection (read-only)

Run on an approved Katcha operator environment, using the configured
PostgreSQL, Temporal namespace and R2 media bucket:

```bash
cd ~/src/katcha
PYTHONPATH=src python scripts/reconcile-unlinked-direct-media.py <SOURCE_ITEM_UUID>
```

The plan checks all requirements:
- Source is `ingesting` **or `failed`** (after Temporal retries are exhausted),
  has no linked Clip, and is **not** part of the
  separately leased remote acquisition workflow
- Proof has the exact source/workflow identity, valid structured stage and raw
  keys, the canonical source hash and a matching durable DomainEvent
- The exact recorded Temporal execution has a verifiable terminal run ID
- No Clip already owns the claimed SHA (collisions require manual review)
- Both bounded stage inventories are fully readable and empty
- `raw/` reports the claimed size, and a complete streaming read matches its
  SHA-256 (maximum 4 GiB)
- A fresh seekable remote-media probe confirms the stored bytes still contain
  a video stream
- Source metadata is unchanged across inspection

The default command makes **no mutations** and returns
`retry_authorized: false` regardless of whether a recovered Clip is eligible.
It prints an evidence-bound approval fingerprint only when all checks pass.

Full SHA-256 verification and video probing involve R2 object reads and
associated storage operations. This is not a free metadata lookup; review
network/storage costs before using it on large footage.

## Explicit recovery commit

After reviewing the exact plan and source provenance:

```bash
PYTHONPATH=src python scripts/reconcile-unlinked-direct-media.py <SOURCE_ITEM_UUID> \
  --apply --approve-fingerprint <64-CHARACTER-PLAN-FINGERPRINT>
```

The operation revalidates the entire external plan, takes a SourceItem DB
row lock, verifies the unmodified proof and original canonical URL, checks
the matching audit event and SHA uniqueness, then creates the Clip and updates
the SourceItem to `ready` in **one database transaction**. It writes the
verified evidence fingerprint and a recovery DomainEvent.

If a durable direct-promotion proof remains unresolved, ordinary ingestion
is blocked *before downloading again*. Do not clear the proof manually to
retry, since existing R2 footage might still be recoverable. Terminal-source
staging cleanup also preserves the proof-linked stage and all attempt stages
for a failed source, including incomplete multipart uploads.

It does not call Temporal start, mutate or reupload R2 media, restart polling,
publish footage, change rights or channel authority, or erase history.
Repeated or stale approvals are rejected.

This is a narrow recovery path, not a general orphan adoption tool:
- Claims missing, malformed, or associated with a different source are blocked
- Running, missing, or unreachable Temporal executions block recovery
- A stage object still present, unfinished multipart uploads, failed/missing
  R2 verification, or any existing Clip hash collision block recovery
- Remote leased acquisition staging uses separate generation/quarantine rules
- Previously created orphans **without a durable checkpoint remain unresolved**

This logic is exercised by local offline/DB fixture tests; it has **not**
passed live OCI worker + R2 + Temporal restart acceptance. Merging code does
not deploy production or enable the disabled poller.
