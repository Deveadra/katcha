# OCI recovery acceptance milestone 1: capacity and cost decision

Status: **complete — conditional capacity, no zero-charge launch guarantee**.
Evidence refreshed 2026-10-08 23:53 UTC. This is a read-only decision; no compute
instance or volume was launched, resized, attached, stopped, or deleted.

## Decision

An additional temporary `VM.Standard.A1.Flex` can potentially be provisioned by
this active `FREE_TRIAL` tenancy because the account's Compute service limits
exceed the currently allocated primary. It **cannot be certified in advance as
an additional always-free instance with no charge**. Oracle grants 1,500 A1
OCPU-hours and 9,000 A1 GB-hours per month; an Always Free-only tenancy's
equivalent concurrent baseline is 2 OCPUs and 12 GB. The production primary
already allocates that full baseline. A temporary A1 on this Free Trial account
may fit within unused monthly hours, but usage reporting can lag and any excess
may consume trial credits. Shape listing, service limits, and the absence of
current charges do not establish host capacity or a future zero-charge bill.

The storage constraint is as important as compute: the tenancy has 150 of 200 GB
of combined boot/block storage allocated. A 50 GB boot volume would consume the
last 50 GB of the free storage allowance. The candidate must not attach any
production volume. While it exists, there would be no free storage headroom for
a fresh cross-AD recovery volume. That would reduce recovery options even though
the running primary and its volumes remain untouched. **Do not launch a parallel
A1 candidate under a no-impact/no-charge requirement.**

## Verified account evidence

| Check | Read-only result |
| --- | --- |
| Home region inventory | 2 compartments and 3 ADs; one running production instance |
| A1 allocation | 2 OCPUs and 12 GB RAM; zero remaining against the Always Free-only concurrent baseline |
| Storage | 3 boot/block volumes; 150 GB allocated of 200 GB; 50 GB remaining |
| E2 Micro | 0 of 2 free-instance slots used; shape listed in `OZDH:US-ASHBURN-AD-3` |
| Subscription metadata | `ACTIVE`, `FREE_TRIAL` |
| Compute service limits | A1 regional 16 OCPUs / 96 GB; AD entries 41 OCPUs / 277 GB; these are provisioning limits, not free allowances |
| October usage report | A1 124.8367 OCPU-hours and 749.0206 GB-hours; reported computed amount 0 for both SKUs |
| Recovery/spend switches | `KATCHA_OCI_RECOVERY_CONFIGURED`, `KATCHA_EXTERNAL_COMPUTE_ENABLED`, and `KATCHA_OCI_PAID_FALLBACK_ENABLED` all `false` |

The usage API grouped the report into the October 1–November 1 bucket. It did not
report a freshness cutoff for the underlying metering, so the quantities and
zero computed amount are historical evidence rather than a guarantee for a new
instance. The verified capacity audit itself prints
`E2_micro_identity_probe_eligibility: POSSIBLE_NOT_BILLING_GUARANTEED` and
`parallel_A1_probe: BLOCKED_BY_ALWAYS_FREE_ALLOWANCE`.

## Conservative arithmetic

October has 744 hours. If the primary runs at 2 OCPUs and 12 GB for the *entire*
month, it uses 1,488 OCPU-hours and 8,928 GB-hours. That leaves only 12
OCPU-hours and 72 GB-hours of the monthly free amounts for any other A1 usage.
For example, a one-hour 1 OCPU / 6 GB probe would use one OCPU-hour and six
GB-hours. These figures assume no other A1 usage and do not override billing
metering, trial-credit policy, an image's licensing terms, or the storage impact.

Oracle's current [Always Free resource rules](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
specify the A1 monthly amounts, two E2 Micro instances, home-region requirement,
and 200 GB combined boot/block storage allowance. The same page describes the
default 50 GB boot volume and says usage above Always Free limits may be charged
on upgraded accounts. This account is a Free Trial, so the trial-credit balance
and usage lag require a fresh account-specific cost check immediately before any
approved test.

## Next acceptance step

The smallest isolated identity probe would be a separately approved, short-lived
1 OCPU / 6 GB A1 or E2 Micro instance with one 50 GB boot volume, a free-eligible
image, private networking, no data disk, no production bootstrap, and no
production leadership. Before proposing an exact launch, verify the image's boot
minimum and licensing, selected subnet and egress, live quota and capacity,
trial-credit balance, and current A1 usage; define a strict runtime limit and
review the temporary loss of storage headroom. Vault-content retrieval and
cleanup require the explicit approval boundaries already set for this task.
