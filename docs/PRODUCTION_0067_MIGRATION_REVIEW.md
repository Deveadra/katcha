# Reviewed 0066 → 0067 production release

The optional `approve-additive-through-0067` policy permits exactly the added
`0067_acquisition_human_recovery.py` migration and, when present in the release
comparison, the reviewed `migrations/env.py` import of acquisition job models.
The executor pins both target blobs and the previous bootstrap blob. It rejects
other schema files, changed migration contents, unknown future heads, and jumps
from any database revision below the verified 0066 handoff schema.

The upgrade adds `acquisition_challenges` and `acquisition_browser_commands` and
expands the existing acquisition-job status check to admit `waiting_human`.
Existing columns and records are retained. New browser command payloads remain
encrypted by the existing application; deployment approval does not grant any
provider, browsing, media, publishing, or credential permission.

Use the normal [manual deployment checklist](PRODUCTION_MANUAL_DEPLOYMENT_RUNBOOK.md).
After exact-SHA CI and image publication, refresh the root-owned deployment tools
from accepted main, then run `--diagnose-release` and `--plan-release` for the
same immutable target. The planner must report the actual 0066 starting revision
and `candidate_migration_policy: approve-additive-through-0067`. The signed
GitHub request must carry that exact policy; the default remains `refuse`.

Before cutover, the executor verifies the full 0066 schema and absence of both
new tables. After cutover, it verifies revision0067, the full preceding schema,
the expanded status constraint, both new tables' columns, nullability, keys,
checks, indexes and defaults. Later migration-free releases use `refuse` and
must retain this verified0067 schema and immutable migration inventory.

Pending Temporal activities still have to drain; explicit workflow review,
backup and isolated restore evidence, fencing, both deployment gates, and
exact image provenance remain required. A migration-bearing deployment is
forward-only once cutover begins: failed postflight requires supervised forward
repair, never restarting an old image or downgrading a populated recovery table.
This policy is a reviewed capability, not authorization to deploy any release.

If an executor attempt is rejected before a deployment audit directory exists,
inspect its root-private `requests/nonce-<nonce>.json` receipt under
`/srv/katcha/deployments`. Updated tooling records a controlled `diagnostic`
with a fixed code, operation and explanation. For example, `HOST_DISK_FULL`
with `docker.pull` identifies capacity failure while staging an immutable image.
Raw process output, command arguments, environment and signed request payloads
are excluded. Unknown/legacy executor output remains
`EXECUTOR_REJECTION_UNCLASSIFIED`; a zero exit without exact-release success
attestation remains `EXECUTOR_OUTPUT_UNVERIFIED`. The consumed request stays
consumed; diagnosis never grants permission to replay it. Review and correct the
specific failure before requesting a new signed attempt.
