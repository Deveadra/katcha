# Ingest startup recovery: Temporal evidence first

The ingest worker previously called `start_workflow(...,
ALLOW_DUPLICATE_FAILED_ONLY)` for persisted SourceItems in **both**
`registered` and `ingesting` states. This can silently replay a failed
execution that had already written R2 media before the process died.

## Revised startup behavior

| Saved source state | Temporal evidence | Startup action |
|---|---|---|
| `registered` | New or already present | Existing registration dispatch and duplicate handling |
| `ingesting` | `RUNNING` | Observe only; **do not dispatch another ingest** |
| `ingesting` | `FAILED`, `COMPLETED`, `CANCELED`, `TERMINATED`, `TIMED_OUT` | Hold for reconciliation; do not dispatch |
| `ingesting` | Execution `NOT_FOUND` | Hold; do not assume safe replay |
| `ingesting` | Temporal unavailable or status not verifiable | Hold; do not infer a terminal state |
| `ready`, `failed` | Any | Existing worker does not start a new ingest |

The recovery guard persists a structured `source_metadata.ingest_recovery`
diagnostic and emits `source.ingest_recovery_required` DomainEvent records.
It uses a fixed error code, the exact recorded workflow ID, enumerated
Temporal status, visibility-recheck classification, and first-observed time.
It does not log raw Temporal exception text or provider credentials. Repeated
startup observations of the same condition are idempotent; recovery conditions
are fenced to the same SourceItem/workflow identity and rechecked under a
database row lock.

Successful ingest clears the obsolete diagnostic while preserving established
source/channel/editorial metadata. Observing `RUNNING` clears a previous
*startup-observation* diagnostic but does not verify actual download completion.

## Manual incident assessment

Start with the read-only evidence collector documented in
`docs/INGEST_RECOVERY_EVIDENCE_INSPECTION.md`. It inspects the exact
SourceItem/Temporal/Clip/direct-staging evidence and never authorizes replay.

1. Confirm the affected source ID and **stored** `workflow_id` from the
   SourceItem. Check the `source_metadata.ingest_recovery` record and source
   audit events. Do not interpret `ingesting` alone as confirmation the
   job is running.
2. Inspect that exact workflow in the configured Temporal **namespace** and
   task queue. Record execution status, run ID, event history, retries, and
   terminal failure cause. An unavailable Temporal server is unknown, not
   proof that the execution is missing.
3. Inspect source-linked Clip rows, `raw/` storage keys, completed staging
   objects, and incomplete multipart uploads before any replay decision.
   Correlate the stored SHA-256 and actual bytes when available.
4. For direct-source staging, use the read-only inventory described in
   `docs/DIRECT_MEDIA_STAGING_RECONCILIATION.md`. That tool only reclaims
   aged staging from sources already `ready` or `failed`; it **intentionally
   retains `ingesting` and unknown entries**.
5. Reconcile the correct persisted source/Clip/Temporal state through the
   reviewed existing recovery procedure. A new attempt must have a distinct,
   reviewed execution identity, preserve any verified uploaded Clip, and
   retain channel/permission lineage. This guard does not mint a new retry ID
   or declare media valid by itself.

No production task, CLI command, automatic replay timer, schema migration,
poller change, or R2 cleanup is introduced by this recovery guard.

## Acceptance boundaries

Unit and database-backed tests validate statuses, idempotent audit records,
workflow identity fencing, and racing READY transitions. Live-Temporal
restart, production database recovery, object-store orphan verification and
3 GiB source acceptance are **not** inferred from fixture-only success.

Leased remote acquisition jobs under `acquisition-staging/` have an
independent owner/generation and outcome quarantine protocol; do not conflate
their recovery with SourceItem direct-ingest workflow recovery.
