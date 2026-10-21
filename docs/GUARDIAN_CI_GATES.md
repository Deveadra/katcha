# Guardian required CI gate

Verified: 2026-10-20. Applies to the `Deveadra/katcha` default branch `main`.

## Source of truth

The repository ruleset [Guardian](https://github.com/Deveadra/katcha/rules/23940002)
is **active** for `~DEFAULT_BRANCH`. Its required invariant is:

- Keep `deletion`, `non_fast_forward`, and `pull_request` restrictions.
- Require the `PR Gate` check from the **GitHub Actions** integration (ID `15368`).
- Enforce `strict_required_status_checks_policy: true`, so a branch must be
  current with `main` to merge.
- Keep `do_not_enforce_on_create: false` and `bypass_actors: []`.
- Do not confuse branch protection's classic `required_status_checks` field
  with a repository *ruleset*. Review the active ruleset itself.

The [CI workflow](../.github/workflows/ci.yml) defines a stable `PR Gate` job
with `if: always()`, dependent on all affected CI jobs. Its aggregate step
fails for failed, cancelled, or timed-out selected checks; unrelated jobs may
be skipped by change planning. Do **not** require individual conditional job
names globally, since unrelated PRs legitimately skip them.

[Application launcher](../.github/workflows/launcher.yml) separately runs
full-startup acceptance on launcher-related PRs and on `main`. That job
must pass on affected PRs before merge. The always-present global gate is
`PR Gate`.

## Read-only ruleset audit

With an authenticated GitHub CLI account authorized to inspect this repository:

```bash
gh api repos/Deveadra/katcha/rulesets/23940002 \
  --jq '{enforcement, target, conditions, rules: [.rules[] | {type, parameters}], bypass_actors}'
```

Verify all invariants above, including the **source integration**. The
expected required-check parameters include:

```json
{
  "strict_required_status_checks_policy": true,
  "do_not_enforce_on_create": false,
  "required_status_checks": [
    {"context": "PR Gate", "integration_id": 15368}
  ]
}
```

If the ruleset drifts, restore the reviewed settings in GitHub before
accepting a new release. Never bypass the rule to compensate for CI failures.

## Acceptance evidence

- **Failing check blocks merge:** [canary PR #508](https://github.com/Deveadra/katcha/pull/508)
  was deliberately non-draft and introduced an invalid Python file *only on
  its disposable branch*. The [CI run](https://github.com/Deveadra/katcha/actions/runs/39441969419)
  failed both `python` and the required `PR Gate`. GitHub returned
  `mergeable_state=blocked` despite `mergeable=true`. The canary was
  **closed without merging**, so the invalid file never entered `main`.
- **Green gate:** A green exact-head documentation PR should be accepted via
  the normal GitHub merge path after CI completion, with no bypass. Its
  accepted commit and check receipts should be recorded in
  [issue #408](https://github.com/Deveadra/katcha/issues/408).
- **Pending/missing gates:** The same canary reported `blocked` while
  `PR Gate` was still pending, before its failed result. The active ruleset
  is strict, so an absent or stale required context also remains ineligible.

This is an administrative CI control, not a production deploy permission.
A green PR does not bypass release readiness, database checks, or rollout gates.
