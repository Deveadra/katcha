# OCI identity probe: planning acceptance

The live [A1 capacity and cost decision](OCI_A1_CAPACITY_COST_DECISION.md) found
conditional Free Trial capacity but no advance zero-charge guarantee for a
parallel A1. A 50 GB probe boot disk would exhaust current free storage headroom.

## Milestone and evidence boundary

The offline probe-planning milestone is complete when the planner and existing
readiness-audit regression tests pass. Live identity-probe acceptance remains
pending. `PLAN_REVIEWABLE` means supplied metadata satisfies the planning rules;
it is not an independently verified provider result or permission to launch.

Read-only inspection on October 8, 2026, refreshed at 23:29 UTC, established:

| Evidence | Result | Source and limitation |
| --- | --- | --- |
| Recovery, external compute, paid fallback switches | All `SAFE_FALSE` | Fresh GitHub variable read through the readiness and capacity audits |
| Production primary and durable mount | Running; mount present | Fresh OCI inventory, Bastion SSH, and host audit |
| Production backup | Timer active/enabled; latest success 2026-10-08 23:01:24 UTC | Fresh host journal audit; service result `success` |
| Isolated restore | Timer active/enabled; latest success 2026-10-08 15:36:03 UTC | Fresh host journal audit; service result `success`; earlier three-database count was reported in the handoff |
| IAM and Vault metadata | All eleven audited checks `PASS` | Fresh administrator metadata audit; no secret bundle contents read |
| E2 slots and storage | Two slots; 50 GB remaining | Fresh home-region inventory: two compartments, three ADs, one instance, three boot/block volumes |
| A1 allocation | 2 OCPU / 12 GB allocated; no audited headroom | Fresh home-region inventory |
| E2 shape placement | `OZDH:US-ASHBURN-AD-3` listed | Fresh shape metadata; not host-capacity or billing proof |

The installed capacity script was byte-compared successfully with Git commit
`3d08e5862bedb972c54e4a6ea6e3566354ec9881`. The installed readiness script matched
the checkout. The inspected checkout was `43a3fcf5`, which does not contain that
capacity commit as an ancestor. No checkout, reset, merge, or history rewrite was
performed; the installed verified audit was used for the successful refresh.

The OCI administrator session was refreshed through browser authentication. An
authorized three-hour Bastion port-forwarding session was created and its SSH
authentication propagation allowed to complete before connecting. The resulting
private tunnel reached the production host. The session and tunnel are temporary;
the inventory and health evidence above must be refreshed if they expire.

## Proposed probe boundary

Plan exactly one `VM.Standard.E2.1.Micro` identity probe, with a private network
and a 50 GB boot disk, subject to fresh image minimum-size and shape-compatibility
metadata. The audited 50 GB headroom would be fully consumed. Two compute slots
do not establish storage room for two probes. A larger minimum image, a new data
disk, or reduced headroom blocks this plan. Preserve all existing disks, including
unattached disks; do not delete resources to make this arithmetic pass.

Use no production bootstrap/cloud-init, production volume attachment, production
services, or coordinator epoch operations. The checked-in recovery dispatcher
and bootstrap provisioner perform resource mutations and are not probe runners.
An E2 identity probe cannot establish A1 application, full database restore,
same-AD/cross-AD failover, fencing, or paid-fallback acceptance.

## Offline evaluator

`scripts/plan-oci-identity-probe.py` reads one local non-secret JSON object. It has
no OCI client, subprocess calls, credential discovery, or network operations.
It outputs blocker names rather than arbitrary input values. Exit status is `2`
for blocked/invalid evidence and `0` only for a reviewable plan. Authorization and
recovery-acceptance fields are always false, even if input attempts to override them.

Supply these fields from reviewed evidence, never from credential bundles:

| Fields | Requirement |
| --- | --- |
| `observed_at` | Timezone-aware ISO timestamp for the oldest supplied check; at most one hour old, not in the future. This is a local planning freshness policy. |
| `primary_healthy`, `inventory_complete`, `home_region_verified` | JSON `true` only after fresh health and complete inventory checks |
| `account_free_tier_verified`, `billing_reviewed`, `quota_verified` | JSON `true` only after account-specific checks; inventory arithmetic is insufficient |
| `image_shape_compatible`, `private_network_egress_verified`, `iam_metadata_verified` | JSON `true` only with reviewed metadata for the selected image, subnet, and candidate identity scope |
| `KATCHA_OCI_RECOVERY_CONFIGURED`, `KATCHA_EXTERNAL_COMPUTE_ENABLED`, `KATCHA_OCI_PAID_FALLBACK_ENABLED` | JSON `false` from fresh switch reads |
| `micro_slots_remaining`, `storage_remaining_gb` | Nonnegative finite inventory numbers; slots must be integral |
| `image_minimum_boot_gb`, `planned_boot_gb` | Positive image minimum and integral planned size, at least 50 GB and at least the image minimum |
| `shape` | `VM.Standard.E2.1.Micro` |
| `availability_domain`, `shape_availability_domains` | Selected AD string and list of AD strings containing it |
| `attach_production_volume`, `assign_public_ip`, `production_bootstrap` | JSON `false` |
| `additional_data_gb` | Numeric zero |

Run from the repository root:

```bash
.venv/bin/python scripts/plan-oci-identity-probe.py /path/to/non-secret-metadata.json
.venv/bin/python -m pytest -q tests/test_oci_identity_probe_plan.py tests/test_oci_recovery_readiness_audit.py
```

Do not relabel the old audit as fresh or fill unknown booleans with `true` to get
a passing plan. The evaluator validates supplied assertions, not their provenance.
Record evidence references separately for review. It neither reserves capacity
nor guarantees cost; recheck the provider state immediately before any approved
launch.

## Next approval boundary

Inspect the exact image, subnet/egress, quotas, account billing, and scoped IAM
metadata for an E2 candidate. Produce a concrete launch proposal
identifying those resources, boot size, bootstrap payload, expected cost, runtime
limit, and retention plan. This milestone does not supply those unverified IDs.

Launching the probe requires explicit approval. Accessing the four Vault bundle
contents requires separate explicit coverage in that approval; do not retrieve
them while gathering metadata. Deletion/cleanup also requires approval, so retain
any subsequently created resources until authorized. IAM changes and recovery
enablement remain separately gated. Preserve production services, all existing
volumes, and Git history throughout.
