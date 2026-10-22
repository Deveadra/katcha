# Direct-to-R2 staging reconciliation

The direct HTTPS ingestion workflow streams incoming bytes through 8 MiB S3/R2
multipart uploads using keys of the form:

`staging/direct/<source-item-uuid>/<attempt-uuid-hex>.<media-extension>`

Successful ingestion promotes verified media to `raw/...` and deletes its
temporary stage. A worker crash between multipart completion and cleanup can
leave temporary objects behind. Interrupted multipart uploads may leave
incomplete uploaded parts until the provider's lifecycle expiration.

## Conservative inventory

From the repository's configured, authorized Python environment:

```bash
cd ~/src/katcha
PYTHONPATH=src python scripts/reconcile-direct-media-staging.py
```

This is **read-only**. It inventories completed staging objects and incomplete
multipart uploads separately. By default it examines up to **200 entries per
inventory**, reports at most 20 structured candidate keys, and only marks
entries older than **24 hours** as eligible. The two lists may not represent a
complete bucket; increase `--max-records` (hard limit: 2,000) for further
bounded inspections. Repeat scans if the inventory is larger. Never interpret
an empty result from a capped scan as proof the whole bucket is clean.

```bash
PYTHONPATH=src python scripts/reconcile-direct-media-staging.py --min-age-hours 48 --max-records 500
```

The inspector only examines `staging/direct/`, not
`acquisition-staging/` used by the separately leased remote acquisition
framework and not the content-addressed `raw/` objects.

Eligibility requires ALL of:

- Recognized exact direct-stage key structure and a timezone-aware S3 last
  modification/initiation time *older* than the configured cutoff
- Existing matching SourceItem in `ready` or `failed` state
- No Clip whose `storage_key` references the stage object
- No pending `direct_promotion_proof` that could depend on this stage.
  Failed sources with a claim retain **all** their attempt staging; a READY
  source with a Clip still retains the exact claimed stage key until reviewed.
- Fresh source-state and Clip-reference recheck under a database row lock before
  any actual deletion or abort

Unknown, malformed, active/`ingesting`, provenance-claimed, registered, recently touched, or
already referenced objects are **retained**. Sources stuck in `ingesting`
after a crash need a separate Temporal/SourceItem incident reconciliation
before any cleanup. This command **does not** declare them failed or restart
any work.

## Explicit execution

Only after reviewing inventory, production leadership, and current worker
state, the same command can be invoked with explicit `--apply`:

```bash
PYTHONPATH=src python scripts/reconcile-direct-media-staging.py --min-age-hours 48 --max-records 200 --apply
```

The command checks Katcha's existing object-storage mutation authority and
uses the configured S3/R2 credentials. It needs permission to list bucket
objects and multipart uploads, delete temporary objects, and abort multipart
uploads. It does not change rights, user accounts, deployment state, Temporal
workflows, the production poller, or provider permissions. No timer or cron
entry is installed.

Failures to list or mutate are surfaced as errors, not silently counted as
successful reclamation. Incomplete multipart parts can also be reclaimed by
provider lifecycle policies; provider expiration does **not** automatically
remove a *completed* temporary stage object.

## Remaining acceptance

This cleanup is intentionally operator-invoked, not an autonomous claim of
exactly-once recovery. Production proof still requires running a supervised,
authorized large transfer against the intended R2 bucket and validating worker
restart, source lease/finality, throughput, orphan state, measured memory, and
standard Clip provenance. The leased remote-acquisition workflow requires its
own evidence-based staging cleanup and recovery gates.
