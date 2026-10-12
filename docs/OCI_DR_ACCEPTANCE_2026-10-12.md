# Katcha OCI autonomous disaster recovery: acceptance record

**Assessment date:** 2026-10-12 (UTC context). **Decision: NOT LIVE-ACCEPTED.
Autonomous OCI disaster recovery remains disabled.**

The accepted infrastructure audit and existing successful R2 backup and
isolated PostgreSQL restore evidence are **not** end-to-end failover evidence.
An E2 Micro identity probe has not been launched or approved. The primary's
A1 capacity is fully utilized; the final 50 GB of home-region free storage
would be consumed by a single 50 GB probe boot volume.

## Evidence grading

| Milestone | Evidence class | Outcome / limitation |
| --- | --- | --- |
| Production OCI A1 primary, Bastion, durable mount | Previously live-audited, operator-supplied baseline | PASS at prior audit; **not freshly re-audited by this report** |
| Four ACTIVE Vault bundles; restricted defined-tag dynamic group and exact four grants | Previously live-audited **metadata only** | PASS for policy shape, **no instance-principal data-plane proof** |
| Hourly R2 database backups | Previously live-audited, operator-supplied baseline | PASS at last audit; current object freshness must be rechecked |
| Weekly isolated three-database PostgreSQL restore | Previously live-audited, operator-supplied baseline | PASS at last audit; **not** proof of OCI candidate restore |
| E2 Micro unused allowance slots and 50 GB free storage | Prior read-only provider inventory | Two slots/50 GB reported; region availability and billing **not guaranteed** |
| Isolated E2 probe planning, launch refusal and cleanup guards | Offline tests and metadata planning | IMPLEMENTED, **not launched**, no live cleanup evidence |
| Untagged denied and tagged four-bundle Vault principal acceptance | Offline mock tests | Guest probe prepared; **live negative and positive reads unverified** |
| Same-AD A1 candidate, cross-AD R2 volume restore, bounded paid selection | Repository mocked and integration-contract tests | Offline-tested; no live replacement instance or restored application |
| Authority fencing, stale-epoch rejection, idempotent incident coordination | Cloudflare state-machine and Python tests | Offline-tested; real replacement handoff and stale-host fencing unverified |
| Failure recovery, rollback, uncertain OCI create, and cleanup | Python regression tests | Offline-tested; **real rollback and cleanup not demonstrated** |
| Cloudflare health watchdog, bounded retries and GitHub recovery dispatch | State-machine/workflow CI | Offline-tested; end-to-end provider delivery and Telegram receipt unverified |
| Production failover / failback and RTO/RPO | No authorized game-day | **NOT TESTED**; no availability, restoration, billing or RTO guarantee |

## Accepted implementation changes

- [PR #380](https://github.com/Deveadra/katcha/pull/380):
  independent recovery activation gate in GitHub and Python; disabled-mode
  cleanup remains possible. Merged and passing exact-head PR CI.
- [PR #381](https://github.com/Deveadra/katcha/pull/381):
  guest-side untagged/tagged instance-principal Vault probe, fifth forbidden
  secret check and operator runbook. Merged and passing exact-head PR CI.
- [PR #382](https://github.com/Deveadra/katcha/pull/382):
  bounded OCI command timeouts, unknown provisioning outcome classification,
  resource-preserving reconciliation and tests. Merged and passing
  exact-head PR CI.
- [PR #383](https://github.com/Deveadra/katcha/pull/383):
  proposed incident dispatch claim, retry cap/cooldown, operator failure
  notification and watchdog contract tests. **Consult merged/CI status before
  treating as accepted `main`**.

Each software change is a non-disruptive repository change. CI exercises
contracts, mocks, isolated test containers and/or Worker state logic; none
establishes live OCI recovery or guaranteed R2 restoration.

## Strict operational posture

1. Keep `KATCHA_OCI_RECOVERY_CONFIGURED=false`,
   `KATCHA_EXTERNAL_COMPUTE_ENABLED=false`, and
   `KATCHA_OCI_PAID_FALLBACK_ENABLED=false`. Do not run the recovery
   dispatcher, modify Cloudflare authority, reassign production volume,
   create a paid fallback, or alter the running primary.
2. Recheck OCI home region, all compartments and ADs, current A1/E2 usage,
   boot/block volumes including unattached disks, billing model, quotas,
   image min-size and shape compatibility, private subnet/NSG, current
   pricing, primary health, R2 backup age and isolated restore result.
   Expired evidence must not be relabeled as current.
3. The staged identity-only probe is **one** `VM.Standard.E2.1.Micro` in
   `us-ashburn-1`, `OZDH:US-ASHBURN-AD-3`, Oracle Linux
   `Oracle-Linux-9.8-2026.09.18-0` (verify still offered), existing
   `katcha-prod-ad3-private` subnet and inspected Bastion-only admin NSG.
   One 50 GB boot disk, **no other storage**, no public IP, no userdata,
   no production mounts or services. Name/tag uniquely as an isolated probe;
   do not tag as recovery candidate at launch.
4. **Cost boundary:** the previously observed 50 GB free remainder falls to
   zero while that boot volume exists. The reported E2 Always Free allowance
   does not guarantee placement or zero billing under the tenancy's
   Free Trial/billing status; collect up-to-date account-specific cost
   evidence and budget alarms **before** asking to launch.
5. Approval must separately cover (a) creating one test instance and 50 GB
   boot volume, (b) if accepted, exact candidate defined-tag mutation and
   four approved Vault reads plus denial against a **known-existing** fifth
   out-of-policy bundle, (c) explicit probe termination and boot volume
   deletion, and (d) any later disruptive A1 failover game-day. No fifth
   secret may be provisioned or invented just to get a green negative test.
6. For launch, record unique probe ID before OCI create. If a CLI timeout
   leaves an uncertain instance or volume result, STOP; reconcile all
   incident/probe-tagged resources and attachments before retry or cleanup.
7. For cleanup, require exact candidate ID and guards in
   `scripts/oci-isolated-identity-probe.py cleanup`. Demonstrate the VM
   terminated, new boot volume deleted, all 50 GB restored to headroom,
   primary still RUNNING, other volumes untouched, all switches still false.

## Stop conditions and rollback

**Before launch:** insufficient storage, image boot min >50 GB, unexpected
subscription/billing status, quota/shape failure, changed network/ingress,
failed primary, stale backup, expired administrator session or incomplete
provenance => no mutation and no retry.

**During identity probe:** unexpected secret access, denied authorized bundle,
candidate identity mismatch, network error mistaken for access denial,
unexpected resource/volume, lost health => stop reads and tagging. Reconcile
candidate state, leave production untouched, and execute **only approved**
test-instance/boot cleanup. Cleanup failure is a separate operator incident;
never delete an unidentified or existing volume.

**Disruptive game-day (not yet authorized):** plan separately for an isolated
A1/ARM equivalent target and R2 restore, both candidate and old-leader
fencing checks, epoch prepare/commit/abort semantics, full application-state
and PostgreSQL/Temporal restoration, lost commit response, public route,
safe return to prior leader, and actual cleanup. Record observed RPO/RTO and
validate no secret leakage or unbounded compute/storage consumption.

## Operational references and next decision

- [Recovery configuration and dispatcher](OCI_RECOVERY_AUTOMATION.md)
- [E2 isolated candidate launch and cleanup](OCI_ISOLATED_CANDIDATE_PROBE.md)
- [Candidate Vault guest acceptance](OCI_INSTANCE_PRINCIPAL_VAULT_ACCEPTANCE.md)
- [Read-only IAM, backup and restore audit](OCI_RECOVERY_READINESS_AUDIT.md)
- [Watchdog retries and notification](OCI_WATCHDOG_DISPATCH_ACCEPTANCE.md)
- [Identity planning acceptance](OCI_IDENTITY_PROBE_ACCEPTANCE.md)

**Authorization dependency:** no user approval exists yet for any OCI instance
launch, defined-tag change, live Vault read, storage creation/deletion,
production authority transition or paid resource. Obtain a separately approved
evidence-backed operation before proceeding. An affirmative approval must name
the allowed step and cleanup policy; general project completion language
does **not** authorize resource mutations.

**Final verdict:** existing primary and backup readiness supported by prior
read-only live evidence; newer software changes validated offline; identity,
R2 restoration, leader fencing, rollback and autonomous recovery are **not
live accepted**.
