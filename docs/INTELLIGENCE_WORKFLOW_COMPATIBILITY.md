# Scheduler history recovery

The growth-refresh and clip-lifecycle activities were added to channel intelligence
without Temporal patch markers. Existing schedules could therefore replay an old
`derive_channel_observations_activity` event while emitting a new growth-refresh
command, leaving the workflow task in a retry loop.

The recovery preserves both observed sequence generations. A frozen epoch of
2026-09-29 UTC separates the audited schedule starts: the older generation began
on September 25 and omitted both activities; the newer generation began on
October 1 and recorded both. This is a recovery boundary between verified
histories, not a claim about the exact deployment time. Do not replace it with
the current date or move it without replaying the affected histories.

For older executions, patch markers preserve the recorded command sequence. The
SDK memoizes that decision, so a legacy execution can retain its old sequence
until continue-as-new. Executions that already recorded the added activities keep
them, and newly started executions record patch markers and include both activities.

Before rollout, offline replay passed against both complete running scheduler
histories (584 and 4671 events), with zero activity or provider calls. The two
regression fixtures retain only each generation's first cycle. Payloads are
replaced with empty JSON, input arguments use fixture values, UUIDs and worker
identities are replaced, and no provider data or credentials are included.

If another running history does not match these generations, stop and inspect its
recorded command sequence. Preserve execution IDs, receipts, and operator choices;
do not reset or terminate a workflow to make replay pass.
