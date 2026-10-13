# Prepare authorized real-media acceptance (KQA-102)

`scripts/kqa102_acceptance_plan.py` prepares a local acceptance plan without
contacting Katcha, providers, storage or production infrastructure. It contains
no execution command. A valid plan, permission declaration or authorization
reference does **not** verify real rights, grant authorization, reserve a budget
or establish production acceptance. Source/provider permissions must still be
checked through existing services when a separately authorized exercise occurs.

KQA-101 remains the prerequisite full-path deterministic acceptance: 100+ visual
requirements, owned media and authorized simulated providers through discovery,
intake, index, measured binding and portable export, including honest blocked and
unknown outcomes. Passing existing slice tests, browser fixtures, matching tests
or ranked-render CI does not establish that complete workload.

## Prepare the plan offline

Copy this template to a new local JSON file. Use the intended channel/project
IDs and saved revision/script digest, the exact accepted release SHA, durable
permission record labels and explicit caps. These are advanced troubleshooting
identities from the existing project and receipt interfaces. Do not paste tokens,
credentials, signed URLs or raw rights documents into the plan. Evidence references
are record labels, not URLs; the tool rejects unknown fields and ambiguous JSON.

```json
{
  "format_version": 1,
  "channel_id": "11111111-1111-4111-8111-111111111111",
  "project_id": "22222222-2222-4222-8222-222222222222",
  "revision": 1,
  "script_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "release_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "operator": "acceptance-operator",
  "authorization_ref": null,
  "sources": [{
    "source_id": "owned-example",
    "channel_id": "11111111-1111-4111-8111-111111111111",
    "project_id": "22222222-2222-4222-8222-222222222222",
    "source_class": "owned",
    "sha256": null,
    "permission_evidence_refs": ["rights:record-to-review"],
    "inspection_authorized": true,
    "acquisition_authorized": false,
    "production_authorized": false,
    "permission_expires_at": null
  }],
  "providers": [{
    "key": "example-provider",
    "version": "v1",
    "max_calls": 3,
    "max_results": 20,
    "max_cost_microdollars": 0
  }]
}
```

`source_class` is `owned`, `licensed` or `reference_only`. Reference-only sources
cannot declare acquisition/production permission. Sources must match the plan's
channel/project. Expiry, when supplied, requires an ISO timestamp with timezone;
this offline tool does not certify freshness. Provider lists may be empty for an
owned-media-only rehearsal. Monetary caps are integer microdollars (1,000,000
microdollars = USD 1), never fractional floats. Planned positive caps do not bypass
existing unsupported-paid-adapter blocks or supply an enforced request ceiling.

```sh
python scripts/kqa102_acceptance_plan.py \
  --input /tmp/kqa102-input.json --output /tmp/kqa102-plan.json
```

The output is labeled `offline_prepared`, records zero executed calls, carries
`live_execution_enabled: false` and `production_acceptance: false`, and includes
an evidence checklist. A stable SHA-256 binds the exact scope, declarations and
caps. Changing rights declarations, revision or budgets creates a different plan
identity. Preserve this identity when requesting separate operator authorization.
Existing output/input files are never overwritten; validation errors leave input
intact and explain the needed correction. Create a new output path after fixing
input or when preparing another plan.

## Separate authorization and evidence for a future live exercise

Before any real call, complete KQA-101 and obtain explicit operator authorization
for the exact plan digest, sources/providers, channel/project/revision and caps.
Current application authorization, media capabilities, rights assessments and
final compile/render checks remain authoritative. Authorization to search does
not grant permission to download, index or render. Verify actual enforced
per-request and total ceilings; unsupported paid dispatch stays blocked.

Record actual search/acquisition receipts and costs, canonical SHA/size/probe
results, rights assessment IDs/versions, measured timestamps/timebase, sparse
coverage, requirement gaps, manual choices, Storyboard binding and export
checksums. Separate observed success from unavailable/denied/expired sources,
reference-only links and remaining unverified paths. Use a private review output
only if separately authorized; this plan tool cannot create or approve one.

After a crash or partial restore, preserve receipt identities and reservations.
A running/unknown provider outcome must be reconciled before any explicit
continuation; never silently retry it, assume it failed or release its budget.
Verify durable ledger recovery and current evidence through the documented
recovery path first. This harness adds no OCI gate, credential, production data
or infrastructure changes.

Run offline proof with:

```sh
pytest -q tests/test_kqa102_acceptance_plan.py
```

Tests forbid sockets and subprocesses and cover channel/project denial, budget
bounds, reference-only restrictions, evidence references, plan identity, input
preservation and refusal to execute or overwrite existing reports. These tests
prove preparation behavior only; KQA-102 remains **not live accepted**.
