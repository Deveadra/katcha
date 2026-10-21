# Inspect a held media ingest without replay

A worker may have uploaded footage to R2, created a Clip, or left a multipart
upload behind before Temporal completed the recorded execution. The startup
guard in `docs/INGEST_TEMPORAL_RECOVERY.md` intentionally holds an
`ingesting` SourceItem rather than repeating a failed workflow.

This inspector gathers **read-only observations**, not a retry authorization.
It never creates workflows, changes database rows, uploads media, deletes
staging, aborts multipart parts, or changes the production poller.

## Command

From the approved Katcha operator environment, with read access to the
configured PostgreSQL database, Temporal namespace and R2 media bucket:

```bash
cd ~/src/katcha
PYTHONPATH=src python scripts/inspect-ingest-recovery.py <SOURCE_ITEM_UUID>
```

The output reports:
- SourceItem state and its exact **persisted workflow ID**
- Temporal visibility and observed execution state, or `not_found`,
  `unavailable` or `not_recorded`
- Linked Clip record, expected SHA-256/length and R2 HEAD/size check
- Bounded inventories of **both** completed objects and incomplete multipart
  uploads beneath `staging/direct/<source_uuid>/`
- Whether SourceItem changed during inspection, a conservative assessment,
  and `retry_authorized: false`

Default inventories examine at most 200 records **per category**, never
unbounded listing. To inventory additional results, use
`--max-records 2000` (maximum). A truncated response is `complete: false`;
a listing error is `visibility: unavailable`. Neither means no stored media.

An R2 HEAD proves existence and reported size only. It **does not** verify
the bytes against the Clip SHA-256. For an explicitly authorized, potentially
large R2 read, run:

```bash
PYTHONPATH=src python scripts/inspect-ingest-recovery.py <SOURCE_ITEM_UUID> --verify-sha256
```

The optional check streams a linked object with bounded 1 MiB chunks and a
4 GiB maximum; it may consume network time and R2 operations, and does not
search for unlinked raw objects.

## Assess the result

| Assessment | Meaning |
|---|---|
| `temporal_running_no_replay` | The exact recorded Temporal execution was RUNNING; do not dispatch a duplicate |
| `media_or_staging_requires_review` | Linked Clip or direct staging exists; reconcile byte integrity, lineage and ownership |
| `terminal_requires_unlinked_raw_review` | No linked/staged object was observed, but a prior copy to `raw/` cannot be ruled out |
| `evidence_incomplete` | Temporal or R2 state cannot be fully verified, including bounded pagination |
| `source_changed_during_inspection` | Source state/workflow/Clip changed while checking; start a fresh inspection |
| `remote_acquisition_requires_separate_reconciliation` | Source belongs to leased remote acquisition, with independent job/generation quarantine |
| `not_an_ingesting_source` | Check ordinary source workflow, not held-ingest recovery |

An empty `staging/direct/` prefix is **not proof of absence** of a
content-addressed `raw/` object: direct ingestion may have moved/copied
media from staging to `raw/` before persisting the Clip. The inspector
therefore never automatically concludes a replay is safe.

No signed source URLs, upload IDs, cookies or raw provider/Temporal exception
strings appear in the report. The CLI deliberately treats an unreachable
Temporal service as `unavailable`, not a missing workflow.

## Next supervised recovery step

Reconcile the workflow's exact Temporal run history (including activity
completion/retries), Clip links, full object lineage and any unlinked raw
objects. Confirm source/channel acquisition authority and the active remote
worker/lease protocol. Only then review a distinct retry identity or a
separate, evidence-backed database recovery transaction. Do not merely reset
`ingesting` to `registered` or invoke `ALLOW_DUPLICATE_FAILED_ONLY`.

This is an operator-invoked evidence snapshot. It cannot provide an atomic
snapshot across PostgreSQL, Temporal and R2; another worker may change remote
state between observations. SourceItem is reread after I/O so visible
database changes are marked stale. There is no live-R2/Temporal acceptance
claim until the procedure has been exercised on the intended host with a
reviewed source.
