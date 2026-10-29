# Deploying acquisition challenge recovery

Human recovery is implemented in the application, but requires the reviewed
`0067_acquisition_human_recovery` database revision before that application starts.
Use the existing signed manual production deployment flow, with the exact
CI-published release and `migration_policy=approve-additive-through-0067`.
This policy does not enable automatic deployment or acquisition dispatch.

The policy admits only the added, immutable 0067 migration and the exact reviewed
`migrations/env.py` change importing Telegram models. It verifies the 0064–0066
migration blobs as well. Modified migration files, unknown revisions, additional
Alembic changes, missing artifacts, and a database below the verified 0066 schema
remain blocked. Use `--plan-release EXACT_SHA` to inspect the release without changing it.

The preflight verifies the existing job/transfer schema and absence of both new
recovery tables. The existing deployer then requires a fresh backup, a successful
three-database restore, leadership authority and idle Temporal executions before
the restart. Postflight verifies revision 0067, the expanded job-status constraint,
and exact recovery-table columns, constraints, indexes and defaults. Migration-free
releases after 0067 retain these checks and exact artifact pins.

This is a **forward-only release**. If startup or postflight fails after the
migration begins, retain the audit receipt and repair forward. Do not restart an
old image against an unknown revision or downgrade populated recovery history.

After the schema rollout, follow [Human verification](ACQUISITION_HUMAN_RECOVERY.md)
for separate worker/browser readiness and operator notification configuration.
Keep automatic dispatch off until the admitted channel's real sequence passes:
challenge, waiting job, operator verification, one fenced retry, stored media,
independent validation and channel linkage. An applied schema does not establish
provider access, a verified scene, rights clearance or a successful notification.
