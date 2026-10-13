# Recovering source-search evidence

The repository's hourly R2 backup covers PostgreSQL databases via consistent
custom-format `pg_dump`, manifest checksums and the final completion marker.
It does not back up `source-search-ledger.sqlite` or its WAL. Previously Editorial
used that file under `KATCHA_WORK_DIR` (default `/tmp/katcha`); production Compose
has no explicit work-directory mount. This is repository evidence, not a live
inspection of production environment overrides, mounts or existing data.

Editorial now uses the application PostgreSQL database for authoritative search
claims, integer budget reservations, candidates and append-only receipts. The
existing state machine is shared with SQLite fixtures; there is no provider
retry redesign, alternative index or rights authority. PostgreSQL dumps include
both Editorial plans and search evidence in the same database snapshot. Runtime
never creates a ledger, initializes an identity or falls back to SQLite.

## First cutover from retained SQLite evidence

These are operator procedures, not authorization to change production. Preserve
all original SQLite files, WAL, database dumps, manifests and audit records.
Never construct an empty replacement for missing historical evidence.

1. Stop every legacy API/activity writer, confirm no request is still executing,
   and keep external/provider dispatch disabled. Confirm the actual historical
   path and retain the main SQLite file with its WAL and SHM together. A main-file
   copy alone may omit committed claims. Do not truncate or checkpoint away the
   only retained evidence.
2. Apply the additive `0063_authoritative_search_ledger` migration to the intended
   database through the normal separately approved deployment procedure. It
   creates empty tables, not a new evidence identity. Back up the target before
   cutover and use an isolated target to rehearse this procedure first.
3. Set `KATCHA_DATABASE_URL` to the isolated target. Run:

   ```sh
   python -m katcha.ops.search_ledger_recovery import-sqlite \
     --source /retained/source-search-ledger.sqlite --generation 1 \
     --confirm-workers-stopped
   python -m katcha.ops.search_ledger_recovery inspect
   ```

   Import uses SQLite's backup API with read-only source access, including
   committed WAL. It verifies identity, typed records, scopes, ordered history,
   candidates and budget reservations, preserves original text and event IDs,
   and commits all rows plus the integrity head atomically. It refuses missing
   source/schema, incomplete records, populated targets and unconfirmed cutover.
   Original source bytes are retained. Import remains quarantined. Pre-seal SQLite
   records cannot prove that no entire record group was previously deleted, or
   that all candidates sharing one URL survived. Historical completeness also
   requires retained-source/operator evidence; import does not invent that proof.
4. Compare the report's identity, counts and receipt history with the retained
   source and all saved Editorial preparation references. Confirm no workers
   wrote to SQLite after the snapshot. Record the evidence and exact reviewed
   identity/seal. A stopped running claim is ambiguous: reconcile it as unknown
   with the existing application flow only after its worker has stopped; hold
   its budget and never repeat it automatically.
5. Only after separate completeness review, record activation using the reviewed
   identity and seal, the independently managed positive search recovery
   generation, and a durable completeness record:

   ```sh
   python -m katcha.ops.search_ledger_recovery activate \
     --expected-identity REVIEWED_UUID --expected-seal REVIEWED_SHA256 \
     --generation 1 --expected-generation 1 --completeness-record REVIEWED_RECORD \
     --confirm-workers-stopped
   ```

   This records an operator assertion; it does not prove external facts or
   authorize provider traffic. Configure `KATCHA_SOURCE_SEARCH_RECOVERY_GENERATION`
   to the activated generation. `KATCHA_SOURCE_SEARCH_DISPATCH_ENABLED` defaults
   false. Enabling it requires a separately approved operational decision; existing
   provider policy, rights checks and OCI leadership fencing still apply.

## Restore and the missing interval

1. Keep all API/activity workers stopped and provider dispatch disabled. Use the
   existing approved download/restore procedure into an isolated PostgreSQL
   instance. Verify completion marker, checksums, backup age, database mapping,
   release and Alembic revision. No OCI gates or credentials are changed here.
2. Quarantine the ledger **after every restore**, including a manual `pg_restore`:

   ```sh
   python -m katcha.ops.search_ledger_recovery quarantine
   python -m katcha.ops.search_ledger_recovery inspect
   ```

   The canonical `postgres-disaster-restore.sh` sets this inhibition before it
   reports success. A missing old schema/head remains closed. A manual restore
   bypassing this step is unsupported: PostgreSQL cannot distinguish an old
   internally consistent snapshot from current state on its own.
3. Use a new search recovery generation in the recovered deployment. An old
   generation cannot silently authorize dispatch against a restored head. This
   is independent of OCI leadership/deployment epochs.
4. An integrity seal proves completeness **within the snapshot**, not freshness.
   Enumerate and independently reconcile the entire backup-to-loss interval,
   including calls whose plans were also lost. Restore later retained evidence
   or provider request/account records under separately authorized inspection.
   Preserve reservations and ambiguous outcomes. If any claim, reservation or
   outcome cannot be reconstructed, leave dispatch quarantined. An hourly RPO
   does not justify replaying lost provider calls. Do not activate merely because
   the UUID, checksums, current receipt counts or leadership fence look valid.
5. Activation is permitted only when the reviewed recovered dataset includes the
   complete interval; use the exact inspected identity/seal and retained
   completeness record. The supplied CLI does not reconstruct missing records
   or authorize a reset. If reconstruction requires changing historical rows,
   prepare and independently review a specific recovery import separately;
   do not edit rows/reseal automatically. Inspection and retained evidence remain
   available while dispatch stays closed.

## Failure behavior and operating limits

Every metadata transaction locks the common head before verifying evidence.
Claims commit before provider I/O; completion uses the existing receipt-text CAS.
Crashes retain reservations, and replay never retries running/unknown outcomes.
A missing database, head, identity, table, event, candidate or changed payload
fails closed before provider work. A transaction failure rolls back budgets,
receipts, candidates and seal together. Search dispatch also requires server
permission, a nonquarantined head and matching positive recovery generation.
Cross-channel reads and claims retain the existing scope checks.

The integrity check scans retained evidence in stable order on every metadata
transaction. This intentionally trades throughput for partial-restore detection;
short metadata work is globally serialized, while provider I/O is outside the
lock. The adapter has a fail-closed evidence-row bound (one million by default),
not unlimited throughput. Monitor row growth and metadata latency, archive only
with a reviewed evidence-preserving design, and benchmark deployment load before
raising the bound. This implementation is intended for bounded Editorial search
workloads, not a high-volume distributed analytics ledger.

Required CI uses an isolated PostgreSQL service and fixture providers to prove
independent-worker claims, killed-worker restart, reservations, atomic import,
WAL records, dump/restore, partial evidence and generation/quarantine gates.
These tests do not prove a deployed R2 backup, production recovery, real provider
compatibility or current source rights. No live infrastructure or data is changed.
