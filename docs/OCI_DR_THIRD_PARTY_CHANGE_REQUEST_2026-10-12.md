# OCI Autonomous DR — independent change review and approval request

**Change ID:** KATCHA-OCI-DR-2026-10-13-R2 — responds to independent rejection of R1  
**Status:** **CHANGES REQUIRED / NO-GO / FOR INDEPENDENT RE-REVIEW**. This document is a proposal, **not** permission to execute.  
**Repository:** `Deveadra/katcha`; reviewed initial `main` at `27d24233135241ad0c9c01d7bb61a91a513191c5` (2026-10-12), then re-inspected `612de812d8855a4e17b8234b0ca66a106a60af54` on 2026-10-13. **Final exact accepted release SHA must be recaptured after blocking fixes merge; these are historical source references, not an execution pin.**  
**Owner:** Katcha operator; **approval:** independent security, OCI billing/FinOps, SRE/DBA reviewers **and then explicit final user greenlight tied to the reviewed revision and phases**.  
**Production:** OCI `katcha-prod-primary-001`, home region `us-ashburn-1`, A1.Flex 2 OCPU/12 GB. **No approved change window, maintenance notice or live resource authority exists yet.**

## R2 critical-review reconciliation (2026-10-13)

The independent source-based review **rejected R1**. Reviewers did not approve resource
creation, IAM mutation, Vault data-plane reads, cleanup, paid fallback or production
failover. This R2 revision preserves **NO-GO**.

- **CRITICAL — paid TTL data safety:** accepted `main` at the audit still contained
  `cleanup_expired_paid()` with TTL-triggered `softstop` and `terminate` that
  could stop an expired **active** production recovery leader. Separate blocking
  fix **[PR #400](https://github.com/Deveadra/katcha/pull/400)** prohibits
  SOFTSTOP of running paid leaders and restricts termination to pre-tagged,
  already-STOPPED retired instances after two fresh coordinator/OCI checks for
  a newer active deployment, matching epoch and no pending handover. The new
  retirement-authorization tag requires independently witnessed negative
  old-epoch fencing after handover; read-only coordinator queries are not an
  atomic lease. **NO paid operations or expiry enablement** before exact-head
  tests, merge and independent review of this residual race.
- **PR #393 (already merged)** fixed the cleanup **scheduling** dependency:
  `paid-fallback-expiry` is gated by separate default-off
  `KATCHA_OCI_CLEANUP_APPROVED`, not the recovery activation flag. That was
  a legitimate improvement, **not** live cleanup acceptance. Prior R1 wording
  calling the scheduling dependency unfixed is superseded.
- **Phase 1 cleanup authorization gap:** normal cleanup and exact-resource
  **emergency containment must be approved, staffed and independently executable
  *before* launch**, not in a later Phase 3 grant. The pre-approved emergency
  envelope is restricted to the verified E2 probe and its new, exact 50 GB
  boot volume, even if candidate-tag removal or guest access fails.
  Separate blocking **[PR #401](https://github.com/Deveadra/katcha/pull/401)**
  implements discrete `revoke-tag`, `verify-revoked` and
  `emergency-contain` operations. The emergency path explicitly does not claim
  effective IAM revocation or production readiness.
- **Revocation:** `verify-revoked` requires four successful OCI
  instance-principal **denials**, with propagation retries bounded by 600 seconds.
  Normal deletion needs independently witnessed after-revocation results.
  If unavailable/compromised, use only preauthorized exact emergency
  containment and escalate residual token/security risk.
- **Evidence:** PR #391's earlier two green GitHub checks with eight skipped
  jobs were **documentation checks**, not script/terraform or live acceptance.
  Targeted exact-head `tests/test_oci_paid_ttl_authority.py`,
  `tests/test_oci_recovery.py`,
  `tests/test_oci_isolated_identity_probe.py` and
  `tests/test_oci_candidate_vault_principal_probe.py` plus recovery
  workflow CI are required after these two code PRs are accepted.
  No reviewer signature, account quote, existing fifth bundle, fresh OCI
  inventory, live restore, RTO/RPO or enforceable two-hour resource cutoff
  has been observed.

**Live gating:** neither code PR merges nor a document review can lift NO-GO.
The independently enforced test termination deadline is a **separate outstanding
blocker**; an operator-monitored two-hour target is not a hard resource TTL.
A stage-specific failover/spend plan is also still missing.

## A. Evidence register (what is known versus not known)

| Source / observation | Observed result | Strength and expiry |
| --- | --- | --- |
| Accepted GitHub `main` and Actions as of review preparation | CI run `38275410134`, Cloudflare recovery coordinator `38275410128`, image publication `38275410127`, launcher `38275410146` all **success**, same head `27d2423...`; scheduled OCI recovery run `38281429842` **skipped** | **Fresh repo-only** evidence; does **not** prove real OCI candidate or billability |
| Historical read-only OCI administrator audits documented in `docs/OCI_A1_CAPACITY_COST_DECISION.md` and `docs/OCI_IDENTITY_PROBE_ACCEPTANCE.md` | 2 unused E2 Micro allowance slots, 150/200 GB boot+block used, 50 GB remaining; running A1 2/12; `ACTIVE` `FREE_TRIAL` at last inspection | **Historical only, last cited 2026-10-08**; capacity, account plan, usage and availability MUST be recaptured |
| Historical Vault/IAM and Bastion check | Four ACTIVE authorized bundle metadata records; restricted candidate defined-tag rule, four exact target.secret.id grants; primary running | Metadata checks; **not** live instance-principal allow/deny evidence |
| Historical backup/restore check | Hourly R2 backup marker and weekly isolated PostgreSQL restore service success reported | **Not current**; must check backup <3 h, isolated restore <8 days, R2 completion/checksums, PostgreSQL schema before any change |
| Latest production host and Cloudflare routed health, all safety-switch values | **Unverified in this packet**; GitHub Variables API and OCI operator session unavailable through this GitHub-only review connection | Require authenticated live read-only evidence from an operator; absence is a hard NO-GO |
| Exact OCI SKU rates, account credit balance, per-hour storage and networking fees, monthly use, instance placement | **Unknown** | Require OCI official account/price evidence and independent FinOps signoff. Never infer $0 from “Always Free-eligible” |
| Existing candidate machine access to the four bundles and denial for a fifth ACTIVE secret | **Never observed** | Acceptance-only; no test may be marked passing in advance |

Oracle documentation: [Always Free compute/storage](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), [OCI budgets](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm). Oracle budgets are **soft controls**, evaluated about every 24 hours; they are **not** an automatic per-hour kill switch. Provider capacity/price can change. See `docs/OCI_DR_REVIEW_EVIDENCE_TEMPLATE.md` for the reviewer-owned, sanitized evidence log.

## B. Strict scope and stage approvals

**Only Phase 0 is permitted at this time:** code/document review and **read-only** OCI, R2, GitHub/Cloudflare/host audits performed with existing read permissions. No resource creation, boot attachment, stopping, tagging, Vault data-plane access, recovery switch change, external-compute reservation, paid compute, or failover is approved by this document.

After third-party review, request *separately worded* user greenlights for:

1. **Phases 1 + emergency containment approved together BEFORE launch:** exactly one isolated `VM.Standard.E2.1.Micro`, one 50 GB boot volume and private Bastion-only network, with advance scoped authority to revoke, isolate, terminate and delete **only** that identified test VM/new boot volume on incident, and tested independent cutoff/backup operator; no candidate tag at launch.
2. **Phase 2:** instance-principal **four untagged denied reads**, exact scope application of `KatchaRecovery.Candidate=true`, **four authorized bundle reads**, and denial on an **independently confirmed, existing fifth secret** outside the four grants. No secret-body output or storage.
3. **Phase 3 (not separately authorizing emergency cleanup):** invoke the **already approved** normal cleanup after tagged access is revoked and four **mandatory successful post-revocation denials** are observed. Independently confirm production API readiness, original disk inventory unchanged, exact test VM/boot removal, and restored quota.
4. **Phase 4:** a **new revision and separate approved maintenance window** for A1 application/R2 recovery, fenced-epoch transfer and rollback, including any paid fallback. Phase 1–3 success does **not** authorize Phase 4.

A user greenlight must cite the specific approved plan revision, scope, spend ceiling, stop time, approved reviewer decisions, and allowed OCI actions. The earlier request to prepare this plan is **not** that greenlight. An accepted GitHub PR alone is not cloud-execution consent.

## C. Phase 0 — required read-only preflight and evidence

**Authorized actor:** OCI administrator/operator on trusted workstation (not GitHub recovery user); independent reviewer witnesses sanitized output. Protect credentials; never paste IDs, account details, SSH keys or secrets into public GitHub/CI logs.

1. Pin accepted `main` release and confirm full CI and workflow statuses, no conflicting resource work; inspect `scripts/audit-oci-recovery-readiness.py`, `scripts/audit-oci-free-candidate-capacity.py`, `scripts/oci-isolated-identity-probe.py`, Cloudflare coordinator runbook and tests. Do not alter an active WSL checkout merely to collect evidence.
2. Validate OCI `KATCHA_VAULT_ADMIN` security-token session separately from Bastion and run **read-only** IAM/Vault-metadata, home-region all-compartment/all-AD compute and boot/block capacity audits. Cross-check OCI Console `Limits, Quotas and Usage`. Record all **running and stopped** compute, unattached volumes and relevant billing resources. Check `FREE_TRIAL` or changed subscription class, remaining trial credits, billed usage and region. Shape LIST result is **not** a reservation or capacity promise.
3. Revalidate current private AD-3 subnet, one Bastion SSH NSG source `/32`, no public VNIC option, outbound route needed for OCI instance principal and approved software packages; inspect 50 GB pinned Oracle Linux image compatibility, image OCID and minimum boot size. Verify the configured `Oracle-Linux-9.8-2026.09.18-0` image is still offered for E2 Micro. Any changed image/network requires a revised reviewed plan, not automatic substitution.
4. Require primary actual RUNNING/ready plus durable volume UUID/mount, coordinator active ID/epoch, valid fence for active and deny for stale epoch; Cloudflare health and current incident state. No active pending recovery incident or parallel provisioning. Confirm `KATCHA_OCI_RECOVERY_CONFIGURED=false`, `KATCHA_EXTERNAL_COMPUTE_ENABLED=false`, `KATCHA_OCI_PAID_FALLBACK_ENABLED=false`, and `KATCHA_OCI_CROSS_AD_STORAGE_CREATION_APPROVED!=true` from fresh values.
5. Check R2 backup complete marker, manifest SHA-256 per database and age (recovery bootstrap policy <= 7200 s). Verify most recent hourly production backup (<3 h) and isolated weekly PostgreSQL restore (<8 days); include DB count, schema and service results. DO NOT trigger production backup or cleanup automatically during read-only inspection.
6. Confirm exactly four ACTIVE Vault secret OCIDs as **metadata**, their tenancy/compartment, defined-tag dynamic-group matching conjunction and policy conditions, no privileged wildcard. Independently confirm whether a known-existing **fifth** ACTIVE out-of-policy secret can be used as a denial fixture without expanding grants or creating secrets.
7. Obtain OCI account-specific current price/usage evidence and paid/trial policy, expected resource charges, monthly limits, egress/tunnel assumptions, available credits, verified external-compute budget limits, and a **tested independently enforceable** emergency cleanup/deadline mechanism plus named backup operator. Do not depend on a 24-hour OCI budget email to cap a two-hour test.
8. Record observation time, release SHA, operator, reviewer, evidence access location, sanitization proof and PASS/FAIL for each gate. **Old 2026-10-08 data cannot satisfy a 2026-10-12 or later launch.** Refresh immediately before any final approved mutation; cross-AD volume gate permits at most a 15-minute-old storage assertion.

**STOP:** any missing/expired/conflicting evidence, a stale backup, unverified costs, unsupported subnet/boot image, inability to clean up, primary instability, other active candidate, unmatched IAM policy or nonzero unknown charges beyond the approved ceiling.

## D. Phase 1 — exact isolated E2 probe proposal

| Bound | Hard requirement |
| --- | --- |
| Placement | OCI `us-ashburn-1` (tenancy home region), `OZDH:US-ASHBURN-AD-3`; placement must be confirmed available at execution |
| Shape | **One** `VM.Standard.E2.1.Micro` (1 GB memory; identity-only, cannot validate A1 production runtime) |
| Image | Pinned reviewed Oracle Linux `Oracle-Linux-9.8-2026.09.18-0` *only after fresh compatible image-OCID/minimum verification* |
| Disk | **One newly created 50 GB boot volume**, delete with the VM; **no** block data disk, snapshot, backup, volume clone or existing production attachment |
| Network | Existing `katcha-prod-ad3-private` subnet; existing Bastion-only admin NSG; no public IP; no new ingress; verified egress paths |
| Payload | No production cloud-init, no production secrets, no Katcha services, no coordinator call; only approved SSH public key and narrow `KatchaIsolationProbe=true`, `KatchaProduction=false`, unique `KatchaProbeId` freeform tags |
| Identity | Candidate defined tag **absent** at creation; candidate tag applied only during Phase 2 after four recorded denials |
| Operation | One `scripts/oci-isolated-identity-probe.py plan` (read-only), then a separately authorized `launch --approve-launch-and-storage-exhaustion` executed from pinned, CI-passed source |
| Runtime | Target <2 hours **from launch to confirmed boot-volume deletion**; preauthorized exact-resource emergency cleanup, independently tested enforcement mechanism, backup operator and hard incident escalations are **NO-GO prerequisites**. Supervision alone is insufficient |
| Retained evidence | Probe nonce, OCI instance/boot IDs **only in restricted evidence store**, image digest/OCID, lifecycle and network readbacks, primary unchanged, timestamps and observed OCI cost/usage |

**Free-storage arithmetic (from stale baseline, to be reverified):** 200 GB allowance − 150 GB already allocated = **50 GB**. One 50 GB probe consumes all remaining headroom; **zero** additional free GB remains during probe. Never run it simultaneously with a cross-AD fresh-volume rehearsal. If Oracle reports different remaining space or image size, revise/reject instead of creating a larger volume.

**Unknown-outcome rule:** if launch is timed out/ambiguous, STOP (do not launch another). Reconcile instance and boot volumes by the unique `KatchaProbeId`, display name, subnet and account inventory. Creation may have succeeded despite CLI failure; deleting the wrong resource is prohibited.

## E. Phase 2 — Vault least-privilege evidence

* Only on the exact Phase 1 instance after fresh isolation verify and approved guest OCI CLI install. No administrator API private key, GitHub token, R2 secret or production credentials copied into its guest.
* Provide only the four authorized **secret OCIDs** as guest environment inputs in a restricted session. The guest script `scripts/oci-candidate-vault-principal-probe.py` uses `--auth instance_principal`, selects `data."secret-id"` and emits pass/fail status, not the secret body. This is still a live provider secret-bundle read and must be treated as sensitive.
* Run `deny-four` while untagged: four independently observed OCI authorization-denial responses. **NotAuthorizedOrNotFound on a nonexistent secret is not sufficient for fifth-secret acceptance**; errors/timeouts are never counted as denials.
* Have reviewer witness exact `tag-candidate --instance-id <approved OCID> --acknowledge-four-untagged-vault-denials --approve-isolated-candidate-iam-tag` from the pinned isolated runner, read back tag and unchanged primary state. Wait for OCI IAM propagation and distinguish transient failures from a valid denial.
* Independently verify the fifth fixture is an **ACTIVE, known-existing** secret outside the four `target.secret.id` grants and that the candidate does not have another allowed IAM path. If none exists, **fail acceptance and request a revised plan**, do not invent/create one.
* Run `allow-four`: four exact successful ID matches plus explicit denial for the fifth. Audit record must include successful/failing result and observed timestamps, zero exposed secret bodies, policy version and dynamic group metadata. No Cloudflare fence or production leadership mutation in this phase.

## F. Phase 3 — already-authorized cleanup and containment

**The authority to clean up must be included in the Phase 1 greenlight.**
The exact VM/boot IDs become available only after creation; before launch,
preauthorize the unique probe nonce, resource envelope, exact-selector process
and restricted OCI administrator role, with an operator and independent backup
operator able to execute both normal and emergency containment. Phase 2 Vault
tagging remains a separate approval. A successful guest test must not start
until this containment contract is proven operational.

1. From an independently identified administrator session, execute
   `scripts/oci-isolated-identity-probe.py revoke-tag` for the exact VM.
   Preserve all unrelated tags. Metadata revocation output is explicitly
   `DATA_PLANE_NOT_YET_VERIFIED`.
2. On the same guest, run
   `scripts/oci-candidate-vault-principal-probe.py verify-revoked` with an
   explicit maximum 600-second propagation window. **All four** authorized
   Vault bundles must return real authorization denials. Network errors,
   tag disappearance and user attestation alone are not sufficient.
3. After separately witnessed denials, run previously approved exact-resource
   `cleanup --acknowledge-post-revocation-four-denials`. The CLI flag is an
   attestation **to existing restricted evidence**, not itself a Vault query.
4. If the guest or tag cannot be safely revoked, run the **preauthorized**
   exact-resource `emergency-contain --instance-id <exact>
   --expected-boot-volume-id <exact> --approve-emergency-probe-containment`.
   This path must refuse mismatched resources and reports IAM as UNVERIFIED.
   Treat any issued token, leaked credential or stalled OCI termination as an
   active security incident; follow separately reviewed administrator
   containment when the runner itself cannot complete.
5. Perform **independent** before/after inventory checks for all original
   boot/block volumes including OCIDs and sizes, test boot absence, exact VM
   termination, 50 GB recovered storage, production readiness endpoint,
   coordinator active epoch, R2/backup health, recovery switches and actual
   billing. The exact-resource script's OCI RUNNING check does not prove
   production API health or unchanged other disks.

**Hard stop:** the prior two-hour human target is not automatic. Reviewer
must demonstrate an effective, separately executable cutoff/cleanup capability
and escalation to a backup operator before approving the initial launch.


## G. Phase 4 — separately reviewed A1/R2/disruptive game-day (NOT YET SIZED OR APPROVED)

This requires a **new stage-specific authorization and measured billing schedule**, even if Phase 1–3 pass:

1. **New architecture/resource accounting:** primary already consumes 2 A1 OCPU/12 GB. No assumption a parallel A1 is free. A same-AD replacement needs a new ~50 GB boot disk plus temporarily dual VM footprint; cross-AD needs **both** 50 GB new boot **and** ≥50 GB restored data volume (≥100 GB new). Historical free headroom is only 50 GB. Do **not** delete primary disks or reduce redundancy to make a test look free.
2. **Paid fallback design:** independently quote exact shape/SKU (shape and image, OCPUs, RAM, per-hour compute, boot/block GB-month, backup/network/tunnel/egress, currency/tax if applicable) from account Console; set one-incident charge ceiling and concurrency=1, finite TTL, independent operator stop and rollback funding. `estimated_hourly_rate × max_hours + storage + transfer + tax + contingency <= explicitly approved maximum` and verify remaining trial credits/charge method. **All rates, ceiling and resulting USD estimate are presently UNKNOWN and must not be invented.**
3. **Mandatory code gate:** merged PR #393 decoupled paid cleanup scheduling from new recovery, now requiring separately approved default-off `KATCHA_OCI_CLEANUP_APPROVED`. **This is not proof of safe cleanup.** Blocking PR #400 addresses active/pending-leader preservation and requires targeted passing exact-head CI, merge, independent verification of retirement tag/fencing, and a live witnessed non-destructive rehearsal before any paid instance or cleanup-gate activation.
4. **Non-disruptive rehearsal first:** staged isolated A1-compatible VM only with separate capacity/storage approval; proven Vault bootstrap, authenticated and SHA-256 checked immutable R2 restoration, PostgreSQL/Temporal/application state/schema verification, local health, intentionally fenced nonleader behavior, rollback of any newly created data disk without touching primary.
5. **Maintenance-window failover:** operator, second operator, SRE/DBA and billing watch present; freeze conflicting deploys and writers. Define measured backup recovery point, expected max downtime, explicit DNS/Tunnel and Cloudflare checks, health timeout, coordinator active/pending epoch state and cutover decision checkpoint. Before `commit`, old primary must remain authoritative; after `commit`, verify old epoch cannot write, the new epoch can, public routing is healthy, and backup continues on the new leader. **Never set both hosts authoritative.**
6. **Failure ladders:** capacity-unavailable → do not overprovision; candidate readiness failure → abort pending and reconcile volume/old-host restart; uncertain create/attach/detach → preserve all resources, freeze retries; ambiguous coordinator commit → read actual epoch, never blindly revert to old epoch; after confirmed commit a return to prior host requires a **new monotonic epoch** with fencing and health checks. Failed cleanup is a persistent incident with explicit cost tracking.
7. **RTO/RPO and spend evidence:** record real discovery/dispatch, launch, Vault, R2 restore, DB/service readiness, epoch commit, public route, notification, cleanup timestamps; observed RTO/RPO and exact OCI billable resources, plus restore checksum results and operator/approver witness. No target RTO/RPO is promised until live measurements exist.
8. **Promotion to autonomous mode:** a new change review must approve ongoing always-free/paid policy, tested expired-resource cleanup, alerts confirmed received, 24/7 incident procedures, reboot/reconnect/lost-authority behavior, and a persistent cost ceiling. *Do not enable recovery automatically at the end of the game-day.*

## H. Stop/rollback matrix

| Failure | Mandatory response |
| --- | --- |
| Insufficient free boot storage, E2 capacity, image/NSG mismatch, changed account or unknown charge | No launch, record NO-GO; no substitutions |
| Guest IAM unexpectedly allowed untagged or fifth secret | End reads, revoke exact tag if present, preserve evidence, immediate security incident, authorized exact cleanup |
| Vault is unavailable / OCI denies authorized four | Mark FAIL/UNVERIFIED; do not broaden IAM grants or run failover |
| Probe VM termination uncertain | Freeze new tests; investigate exact unique probe ID, boot/VNIC resources and tag state; no blind retry |
| R2 generation invalid or stale, database schema inconsistent | No cutover; retain original primary authoritative; quarantine candidate |
| Candidate readiness or fencing failed | Do not commit; abort pending if coordinator state verifies; repair/restores only with known disk owner |
| Detach/attach, commit or coordinator response ambiguous | Preserve old/new hosts and volume; read back authority and attachment inventory; incident escalation |
| Paid TTL, independent cleanup or notification broken | No paid launch; test fix and independent reviewer signoff |
| Post-commit old leader still authorized | SEV-1 split-brain; freeze external writes and force independently verified fencing, not ad hoc epoch reversal |

No OCI instance, boot/block volume, Vault content, IAM tag, Cloudflare configuration, paid resource, production data or recovery switch is modified by creating/reviewing this document.

## I. Independent review and decision record

Reviewers MUST use the linked [evidence worksheet](OCI_DR_REVIEW_EVIDENCE_TEMPLATE.md), verify source timestamps and independently inspect raw restricted artifacts, then record:

- **Security/IAM:** exact four grants, fifth existing fixture, candidate tag/cleanup, secrets never printed, tag propagation, no privileged IAM changes.
- **OCI/SRE:** placement, boot image/size, all-compartment volume inventory, network and Bastion isolation, primary health, stop/rollback and time-box staffing.
- **FinOps:** subscription/trial credit, real SKU rates, zero-charge claims rejected unless evidence, 200 GB storage accounting, minimum billing intervals, alerts and independent stop plan.
- **DBA/Recovery:** backup freshness, checksums, isolated restore age, R2/Temporal and schema validation, old/new fence safety, measured RTO/RPO requirements.
- **Owner:** approved exact plan hash/revision, operation subsets, maximum tolerable spend and maintenance window.

**Decision choices:** `NO-GO` (default), `CHANGES REQUIRED`, `APPROVE PHASE 1 ONLY`, `APPROVE PHASES 1–3`, or later `APPROVE PHASE 4 REVISION`. Blank/implicit acceptance means **NO-GO**. Even reviewer approval is not the user's explicit final greenlight.

**Review blockers already known:** outdated OCI/billing/health snapshot; no verified existing fifth-secret fixture; **paid cleanup authority/fencing and emergency identity-probe containment fixes pending review**, independent enforced test cutoff unproven, and no complete live A1 cross-AD cost/capacity plan. This change proposal therefore is **not currently executable**.
