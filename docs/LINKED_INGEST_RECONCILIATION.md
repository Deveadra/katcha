# Operator-reviewed linked Clip reconciliation

An interrupted Clip ingest may leave `SourceItem.status=ingesting` after
Temporal becomes terminal. PR #505 prevents unsafe automatic replay, while
`scripts/inspect-ingest-recovery.py` gathers read-only evidence. When a
SourceItem **already links an intact Clip**, this command can reconcile that
record to `ready` without downloading footage again.

This procedure **does not recover unlinked `raw/` objects** or restart failed
Temporal workflows. Merely finding a matching content hash or filename never
authorizes associating a different publisher's content with a SourceItem.

## Step 1: Read-only proposal

Run in an approved operator environment with PostgreSQL, Temporal namespace,
and S3/R2 read access:

```bash
cd ~/src/katcha
PYTHONPATH=src python scripts/reconcile-linked-ingest.py <SOURCE_UUID>
```

The tool verifies all of the following before returning `eligible: true`:

- SourceItem remains `ingesting`, has an existing linked Clip, and does not
  belong to the separately leased remote-acquisition pathway
- The exact persisted Temporal workflow has a known, **terminal execution
  run ID** (COMPLETED, FAILED, TIMED_OUT, CANCELED or TERMINATED)
- The linked Clip has valid video-stream metadata, is not FAILED, and uses the
  expected content-addressed `raw/<prefix>/<sha>.<extension>` storage key
- R2 HEAD reports the expected nonzero length and streamed SHA-256 readback
  verifies **all stored bytes** (bounded by 4 GiB)
- The bounded direct-stage inventories are complete, with no completed stage
  objects or unfinished multipart uploads remaining
- The SourceItem and Clip did not change during evidence collection

The read-only proposal prints a 64-character `approval_fingerprint` bound to
the source state, all source/Clip metadata digests, the exact Temporal run and
outcome, and verified storage/staging evidence. It does not grant publication
rights or confirm requested scene content.

**Cost note:** The proposal reads the linked Clip object through R2 to
validate its SHA-256. This is an explicit 4 GiB-capped read, not a cheap HEAD
request. Avoid unnecessary repeated checks on large files.

## Step 2: Explicit evidence-bound commit

After reviewing the proposal and confirming the operator's authorization:

```bash
PYTHONPATH=src python scripts/reconcile-linked-ingest.py <SOURCE_UUID> \
  --apply --approve-fingerprint <EXACT_64_CHARACTER_FINGERPRINT>
```

The implementation rechecks **all** evidence before attempting a transaction.
A PostgreSQL row lock then verifies the source/Clip identity, state, timestamps
and metadata hashes still match the approved snapshot. If anything changed,
it aborts without a database update.

The only successful mutation is a database transaction that:
- Sets the existing SourceItem to `ready`, keeping its **existing Clip ID**
- Clears obsolete ingest/recovery diagnostic errors and preserves all other
  editorial/channel/rights metadata
- Persists the Temporal run, Clip SHA and operator-approved evidence fingerprint
- Writes one `source.ingest_reconciled_linked_clip` audit DomainEvent

It never starts a workflow, reuploads media, alters R2, deletes staging,
changes permissions, enables the poller or initiates publication. Reusing the
same approval after success is rejected and does not add duplicate events.

A failed/absent Temporal run ID, running workflow, missing or invalid Clip,
unresolved staging, unknown ownership, invalid R2 hash, incomplete listing,
changed source/Clip, or prior completion **blocks** the commit.

## Limitations

There is no atomic transaction spanning PostgreSQL, Temporal and R2. The
operation rechecks a current terminal Temporal execution and byte identity
immediately before its guarded DB transaction, but further live end-to-end
acceptance is needed. It does not convert a failed Temporal history into a
successful execution: follow-up analysis/passthrough must be assessed
separately if that workflow failed after ingestion.

An `ingesting` source with **no linked Clip** is not eligible; separate
human-reviewed origin/provenance evidence must establish the ownership of any
unlinked `raw/` object. Similarly, independent remote acquisition leases and
`acquisition-staging/` are excluded.

Production use requires a reviewed operator execution and a supervised live
Temporal/R2 acceptance run. Merge of this code is not production deployment.
