# OCI recovery milestone 2: isolated candidate probe

Status: **launch-ready, awaiting explicit resource approval**. The local runner
and live read-only preflight were verified on 2026-10-08 at about 19:10
America/Chicago. No probe instance or volume has been created.

This milestone uses `scripts/oci-isolated-identity-probe.py`. Its `plan` and
`verify` modes are read-only. The `launch` mode creates one OCI instance and
its boot volume; the command requires an explicit approval flag and is not run
as part of planning.

## Exact proposed candidate

| Property | Value |
| --- | --- |
| Shape | `VM.Standard.E2.1.Micro` |
| Image | `Oracle-Linux-9.8-2026.09.18-0`, available for that shape; image size 47,719 MiB |
| Placement | Home region `us-ashburn-1`, `OZDH:US-ASHBURN-AD-3` |
| Subnet | Existing `katcha-prod-ad3-private`; public IP prohibited by the subnet and disabled in the launch command |
| Ingress | Existing verified admin NSG, whose only ingress rule is TCP/22 from the current Bastion private endpoint; AD-3 subnet security list has no ingress |
| Boot | One new 50 GB image-based boot volume; no data volume, snapshot, clone, or production volume attachment |
| Bootstrap | Oracle Linux platform image with only the configured public SSH key; no user data and no Katcha cloud-init |
| Tags | `KatchaIsolationProbe=true`, `KatchaProduction=false`, unique `KatchaProbeId`; **no** `KatchaRecovery.Candidate` defined tag |
| Production authority | No coordinator call, Cloudflare change, production service start, epoch change, or recovery switch change |

The primary subnet security list has no ingress rules. The primary's admin NSG
allows only Bastion SSH from one `/32` endpoint. These inspected rules do not
admit database traffic from AD-3. The plain candidate image receives no database
credentials. The runner rechecks these conditions before launch and refuses to
create a second active probe. It also requires an active `FREE_TRIAL`
subscription; an account-model change stops the plan for a new cost review.

The last capacity audit found two E2 Micro slots and 50 GB of remaining combined
boot/block storage. This proposal uses one slot and all 50 GB. The shape listing
is not a host-capacity promise. The [capacity and cost decision](OCI_A1_CAPACITY_COST_DECISION.md)
records the `FREE_TRIAL` account model and billing limits; pricing and available
capacity must be refreshed just before launch. While the probe boot volume
exists, no free storage headroom remains for a fresh cross-AD recovery volume.
The running primary and its current disks are not modified.

## Verification already completed

- The runner's live `plan` returned the pinned image, private AD-3 subnet, two
  remaining Micro slots, and 50 GB storage before / 0 GB after a possible launch.
- Static checks and 110 focused offline regression tests passed. Tests cover
  launch arguments, missing approval flag, production or data volume attachment,
  public IP, wrong subnet, boot size, cloud-init user data, Vault candidate tag,
  primary health, and widened SSH ingress.
- `git diff --check` passed after the source and documentation updates.

## Approval and execution boundary

The user's earlier restriction requires **explicit approval before launching or
deleting OCI resources**. A request to advance the checklist is used to prepare
this concrete plan; it does not silently authorize the instance and boot-volume
mutations. The later Vault-content test, IAM changes, and enabling recovery each
remain separate approvals.

After approval, refresh the administrator session, run `plan` again, and launch
only if the same checks pass:

```bash
.venv/bin/python scripts/oci-isolated-identity-probe.py plan
.venv/bin/python scripts/oci-isolated-identity-probe.py launch \
  --approve-launch-and-storage-exhaustion
```

The launch prints `PROBE_ID` before its OCI call to help reconcile an uncertain
API result; do not retry blindly after a timeout. It then verifies the instance,
50 GB boot volume, absence of data volumes/public IP/user data/Vault candidate
tag, private VNIC and NSG, and the still-running primary. `verify --instance-id`
can repeat that inspection without changing resources.

For a genuinely temporary test, approve termination of **only the newly created
probe instance and its new boot volume** as a separate scoped action after the
probe results are recorded. Preserve all pre-existing volumes and production
services. The runner's `cleanup` mode requires that separate approval flag,
the exact instance ID, matching unique name and tag, one verified 50 GB boot
volume, and no data-volume attachments. It checks that the test boot volume
disappears and reports whether the primary remains running. Cleanup remains
available if launch eligibility or account status changes after the probe starts:

```bash
.venv/bin/python scripts/oci-isolated-identity-probe.py cleanup \
  --instance-id <exact-probe-instance-id> \
  --approve-cleanup-of-test-instance-and-boot-volume
```

Milestone 4 will include fuller provisioning failure diagnostics and cleanup
acceptance. An ambiguous launch or termination result must be reconciled by the
unique probe ID before any retry.

This isolated launch does not prove instance-principal Vault access. The
candidate intentionally lacks the dynamic-group matching tag. Milestone 3 will
require its own controlled tagging and four-bundle test after this isolation
result is accepted.
