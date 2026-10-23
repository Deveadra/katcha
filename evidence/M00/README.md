# M00 evidence: initial implementation slice

These files record the 2026-10-23 baseline review, not completed autonomous
production acceptance.

- `baseline.json`: accepted main, merged acquisition work, individually inspected
  main CI job records, runtime-image workflow status and unprobed live boundaries.
- `local-preflight.json`: output from the new read-only source/configuration
  preflight while this implementation was uncommitted. Its `source_sha` is the
  accepted baseline, and `working_tree_dirty=true` explicitly distinguishes the
  edited code. No live browser or provider probe was requested.

Reproduce the preflight from the repository using its Python environment:

```sh
PYTHONPATH=src python scripts/autonomous_e2e_preflight.py
```

No secret values, provider responses, signed media URLs or live content are
included. File presence proves only local source ownership. The local environment
configuration is not a measurement of production or either channel's admission.

The evaluator's locked input is
`tests/fixtures/semantic_benchmark/manifest.json`, with its canonical digest in
`manifest.sha256`. It is deliberately labeled an evaluator contract fixture:
it has symbolic labels and hashes and no media bytes or provider predictions.
It cannot stand in for M00-C's independent annotations or M06-A's real baselines.

The companion implementation plan records the next vertical slices and remaining
authorization and acceptance gates. M00 and M06-A remain in progress.
