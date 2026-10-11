# KSI-102 — bounded source search foundation

This module implements proposed v1 search intent independently of PR #346's
script parser. It does not wire an API, run AI research, obtain media, grant
rights, or select assets for production. No shared API, models, migrations,
Agent B intake files, or script text are modified.

`SearchIntentV1` accepts a caller-supplied requirement and channel/project/revision
scope. `plan_job` maps existing YouTube, Reddit, and web-scout adapters, producing
deterministic `SourceSearchJobV1` identities. Import/feed adapters are explicitly
not semantic search providers. Unknown versions fail closed. YouTube searches
use relevance without the legacy recency filter. Reddit retains its adapter's
one-year recency ceiling and cannot preserve negative terms; such jobs are
rejected rather than silently weakened.

`SearchJobLedger` saves immutable intent, atomic per-scope/provider reservations,
current status, and append-only receipt events in SQLite. This is an A-owned
activity receipt repository, not a competing project/media database. A caller
must authorize the scope through existing application controls, choose a
persistent local volume, and keep a single authoritative ledger. Separate
ledgers do not coordinate quotas; SQLite WAL requires local disk, not a network
filesystem. Deployment and Temporal/PostgreSQL integration remain later work.

Replayed jobs return the saved result without making another request. Concurrent
claims reserve only once. Budgets count adapter invocations and reserved result
slots, not underlying HTTP requests; the provider's observed operation counters
are retained separately. Monetary amounts reserve a conservative per-call
ceiling and are never refunded automatically. Budget policy cannot change under
an existing scope/provider to bypass exhaustion. Calls with unknown outcomes,
including timeouts or worker interruptions, require explicit reconciliation.
`mark_interrupted` is only for a worker the caller has confirmed has stopped.
Only known HTTP 429 rejections support an explicitly requested retry, respecting
the provider delay, configured attempt ceiling, and remaining reservations. A
delay above one day blocks retries rather than shortening the provider's delay.

External dispatch defaults off. Paid web scouting remains blocked because the
legacy adapter does not prove a per-request cost ceiling; a configured global
AI budget is insufficient for this job contract. This module does not read
credentials. Existing adapters remain responsible for transport timeouts and
their credential behavior; channel-bound credentials require integration review
before live rollout. Adapter and capability injection are trusted scheduler
configuration, never user-controlled API fields.

Receipts retain actual returned source URLs, provider usage, cursors, reservation,
and actionable errors. URLs are references only, without download or rights
promotion. Exception text is not retained. Structured candidate metadata,
publisher verification, source-specific retention, and `AssetCandidateV1` handoff
to Agent B are KSI-103 work, gated on KSI-102 acceptance. Agent B can consume
receipt IDs and the stable requirement/scope identities without changing these
owned files. No Agent B chat was identifiable during the ownership inspection;
no shared-contract change was sent or assumed accepted.

## Executable offline fixture

From this worktree in PowerShell:

```powershell
$env:KATCHA_WORK_DIR = Join-Path (Get-Location) '.local/test-work'
$env:KATCHA_DATABASE_URL = 'sqlite+pysqlite:///:memory:'
.venv/Scripts/python.exe -m katcha.editorial.source_intelligence --fixture --ledger .local/ksi102-fixture.sqlite
```

The fixture explicitly uses synthetic requirements and `example.org` references.
It dispatches 120 fake requests on first execution and zero on replay. Reports
label `fixture_only`, zero real provider calls, and zero verified usable assets.
Actual receipts and replay outputs are saved under `.local/` for local review;
they are not live source-discovery evidence or accepted runtime integration.

## Verification

Run focused and existing adapter compatibility tests:

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_source_search_jobs.py tests/test_discovery_adapters.py tests/test_discovery_http_errors.py tests/test_youtube_discovery.py tests/test_reddit_discovery.py tests/test_web_scout_discovery.py tests/test_feed_discovery.py --basetemp=.local/ksi102-tests
.venv/Scripts/python.exe -m ruff check src/katcha/editorial/source_intelligence tests/test_source_search_jobs.py
.venv/Scripts/python.exe -m mypy src/katcha/editorial/source_intelligence --follow-imports=silent
```

Use a fresh workspace-local pytest base directory when sandbox temporary folders
are inaccessible. Scoped type checking silences existing imports; it does not
certify repository-wide types. Full repository lint currently reports an existing
E501 at `src/katcha/media/transcription.py:26`, outside Agent A's scope. CI and
merge acceptance must be checked on the PR's exact head; no waiver is implied.

Rollback: remove this independent package and its dedicated tests. Do not delete
a deployed ledger or its evidence as part of rollback. No migrations or legacy
call sites are changed by this slice.
