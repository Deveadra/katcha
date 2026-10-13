# OCI DR third-party review: sanitized evidence and decision worksheet

**Change:** [KATCHA-OCI-DR-2026-10-13-R2](OCI_DR_THIRD_PARTY_CHANGE_REQUEST_2026-10-12.md)  
**Current status:** `CHANGES REQUIRED — NO-GO, INDEPENDENT RE-REVIEW PENDING`. Use a restricted evidence repository for private OCI resource identifiers, billing screenshots, Audit event IDs and third-party signatures; link only an access-controlled record here. **Do not upload credentials, secret values, R2 bundle contents, SSH material, raw OCI stderr or unrestricted account data to public GitHub.**

## 1. Identity and version freeze

| Entry | Reviewer fills using witnessed actual result |
| --- | --- |
| Immutable source release SHA at execution (not just PR head) | NOT CAPTURED |
| Change request revision and code-review PR | NOT APPROVED |
| GitHub CI exact SHA, recovery/Cloudflare workflows and branch protection | Initial source `27d2423...`, 2026-10-13 audit `612de812d8855a4e17b8234b0ca66a106a60af54`; PR #393 merged; blocking PRs #400 and #401 **not counted as accepted until merged, tested exact-head, and main reverified** |
| Read-only operator session validated at (UTC), expiry | NOT CAPTURED |
| OCI tenancy/home region and current subscription class | Historical `us-ashburn-1 / FREE_TRIAL`; **requires refresh** |
| OCI cost SKU evidence references and pricing snapshot UTC | NOT CAPTURED |
| Planned resource approver/incident commander/secondary cleanup operator contacts | NOT ASSIGNED |
| Planned launch time, hard operator cutoff, maintenance window (if any) | NOT SCHEDULED |
| Restricted evidence bundle location, artifact checksums and access rights | NOT CREATED |
| Candidate secret/switch state: four IDs and three launch switches | No live values captured for this packet; record **only audit result**, not secret IDs |

## 2. Time-stamped preflight gates

Do not mark PASS from the October 8 inventory or from mocked CI. The source scripts are read-only when invoked with documented audit and `plan` modes; pin them to accepted, tested `main` and never invoke mutation modes during evidence acquisition. Include a timestamp and restricted artifact reference for **every** line.

| Gate | Passing criterion | Status | UTC observation | Evidence reference |
| --- | --- | --- | --- | --- |
| Region/tenancy | Exact home region and correct active OCI account verified | UNVERIFIED | — | — |
| Current pricing | Subscription, free eligibility, usage, trial credits, SKU rates and possible overage inspected by FinOps | UNVERIFIED | — | — |
| Primary | OCI RUNNING, 2 OCPU/12 GB, durable volume UUID/mount, API ready | UNVERIFIED | — | — |
| Authority | Coordinator active epoch and leader agree; no pending replacement; old epoch denied | UNVERIFIED | — | — |
| Switches | Recovery, external compute, paid fallback all `false`; cross-AD creation not `true` | UNVERIFIED | — | — |
| E2 allowance | At least 1 unused slot, AD-3 shape present, placement request possible | UNVERIFIED | — | — |
| All-compartment inventory | All instances including stopped, all active boot/block volumes including unattached | UNVERIFIED | — | — |
| Combined storage | >=50 GB current free for one E2 probe; post-probe >=0 GB; no other simultaneous disk planned | UNVERIFIED | — | — |
| Image | Exact pinned image available and compatible with E2, minimum <=50 GB | UNVERIFIED | — | — |
| Private networking | Bastion-only SSH source, private AD-3 subnet, no public address, required package/OCI egress | UNVERIFIED | — | — |
| Vault/IAM | Four ACTIVE authorized secret metadata, conjunctive candidate dynamic group and four exact grants; primary not tagged | UNVERIFIED | — | — |
| Fifth fixture | Independently confirmed existing ACTIVE secret outside all candidate IAM grants | UNVERIFIED | — | — |
| Backups | Last successful hourly production backup <3 h and latest complete R2 generation/checksums within candidate policy | UNVERIFIED | — | — |
| Weekly restore | Successful isolated PostgreSQL 16 restore <8 days, expected DB/schema count | UNVERIFIED | — | — |
| Alert/cleanup ownership | Notification destination, pre-approved normal AND emergency exact-probe cleanup, independently executable backup operator, boot ID recording and escalation proved **before launch** | BLOCKED | — | PR #401 + independent live rehearsal pending |
| Paid cleanup scheduling | PR #393 merged; independent default-off `KATCHA_OCI_CLEANUP_APPROVED` gate, no activation | CODE COMPLETE, LIVE UNVERIFIED | — | PR #393; verify current main |
| Paid leader fencing/retirement | No SOFTSTOP of expired active paid leader; only STOPPED expired retired candidate with explicit handover tag, newer authoritative leader, no pending, two OCI/coordinator reads | BLOCKED | — | PR #400 exact-head CI, independent safety proof pending |
| Enforcement of test duration | Independent tested hard cutoff + preapproved containment executable despite compromised guest; two-hour target not enough | BLOCKED | — | Not implemented/verified |
| IAM propagation and revocation | Same guest, exact candidate; after tag removal **four required** denials within 600 s; failed revocation triggers emergency containment | BLOCKED | — | PR #401, live denial evidence pending |
| Source test coverage | Targeted exact-head Python recovery, Vault probe, OCI identity and workflow tests; no skipped suite passed off as tested | BLOCKED | — | PR #400/#401 CI required |
| Phase 4 capacity | Measured >=100 GB new cross-AD boot/data or explicit signed paid-storage alternative | UNVERIFIED | — | — |

**Assessment rule:** ANY `UNVERIFIED`, `FAIL`, `BLOCKED` or too-old evidence implies **NO-GO** for the corresponding live phase. A stale audit can be referenced as background only. Never replace absent provider output with an estimate to get a green review.

## 3. Resource and cost ledger (fill before greenlight)

| Resource/action | Maximum proposed live inventory delta | Current quoted unit rate and source | Maximum exposure and cleanup |
| --- | --- | --- | --- |
| E2 identity probe | +1 `VM.Standard.E2.1.Micro`, AD-3 | UNKNOWN — obtain current OCI account quote | **Hard independent expiry unverified — NO-GO**; backup operator and advance exact-VM/boot containment required |
| E2 probe boot | +1 50 GB boot volume, no other volumes | UNKNOWN — show 200 GB shared allowance and billing classification | Delete exact boot disk with instance; preserve existing disks |
| Bastion and network | Existing verified private subnet/NSG, no new public IP/ingress | UNKNOWN — verify no paid change | Zero new gateway/route resources authorized |
| Vault API read | Four untagged denied reads; tagged four matching positive reads and one known-fifth denial; **four mandatory post-revocation denials** | UNKNOWN / verify API and request costs | 600-second bounded IAM propagation, emergency containment on failure |
| R2 transfer | Identity probe: **none**. Later game-day: measure exact backup bytes and operations | UNKNOWN — require Cloudflare/OCI data-transfer and request quote | Bounded isolated dataset only in separately approved revision |
| A1 recovery VM | Phase 4 only; shape/OCPU/RAM/image/time TBD, incremental to running 2/12 primary | UNKNOWN — obtain SKU and actual monthly allowances/trial credits | Explicit TTL, budget, safe decommissioning and return-to-free |
| Cross-AD boot + data | Phase 4 only: default **50 + 50 = 100 GB** additional; 50 GB old available historically | UNKNOWN — account price for excess GB-month | No create until independent headroom/charge approval |
| Contingency and tax | No implied authorization | UNKNOWN | Reviewer sets fixed **USD maximum** before live spend |

**Billing proof rule:** calculate worst case from quoted OCI SKU prices, minimum billing periods and measured duration; include additional storage, trial-credit uncertainty, egress/API fees, tax/FX and contingency. Do not use an invented rate. Explicitly enter a user-approved **USD maximum**; absence means NO-GO. Oracle budget alerts are evaluated about every 24 h and are only a secondary soft warning. A human-monitored time limit is **not** a technically enforced cloud stop.

## 4. Per-phase authorization record (blank until separate approval)

| Stage | Required independent approval and limits | Reviewer verdict | Final user greenlight | Date/time |
| --- | --- | --- | --- | --- |
| Phase 0 read-only | Evidence collection, no mutation | PENDING | Not required for existing read-only audits | — |
| Phase 1 E2 launch PLUS emergency cleanup | Exact region/shape/image/subnet/NSG/50 GB boot **and advance authority** for exact-resource cleanup and emergency containment; independently enforced deadline and backup operator | NO-GO | NOT PROVIDED | — |
| Phase 2 IAM/Vault | Exact candidate, four secret IDs, verified fifth existing fixture, candidate tag and negative/positive auth checks | NO-GO | NOT PROVIDED | — |
| Phase 3 normal cleanup (already preauthorized in Phase 1) | Same guest four verified post-revocation denials; authorized exact boot/VM deletion, independent complete disk inventory and production API health | NO-GO | NOT PROVIDED | — |
| Phase 4 A1/paid/production | New revision, resource/budget/maintenance/change authority, fencing/failback, witnesses | NO-GO | NOT PROVIDED | — |
| Activate unattended DR | Live RTO/RPO evidence, effective TTL/notifications, independent cost containment | NO-GO | NOT PROVIDED | — |

**Reviewers** (fill actual independent names/contact identifiers and approval statements in the **restricted record**, not this public template):

* Security/IAM reviewer: **PENDING** — signature/date/evidence link
* OCI operator/SRE reviewer: **PENDING** — signature/date/evidence link
* FinOps/billing reviewer: **PENDING** — signature/date/evidence link
* DBA/data recovery reviewer: **PENDING** — signature/date/evidence link
* User final phase-specific authorization: **PENDING** — exact change revision, phase and max spend

## 5. Live test acceptance worksheet (do not prefill)

| Test | Required success evidence | Result |
| --- | --- | --- |
| Untagged deny-four | Four distinct existing authorized bundle IDs, explicit OCI denial per ID, no connection-error false positive | NOT RUN |
| Tag candidate | Exact probe OCI ID/tag readback; production instance not tagged | NOT RUN |
| Tagged allow-four | Four exact matching secret IDs, zero body output or logging | NOT RUN |
| Existing fifth denial | Separate administrator proof fifth secret is ACTIVE/exists + candidate denied | NOT RUN |
| Candidate tag revocation | Removal from OCI defined tags plus **mandatory four denied live bundle reads**, bounded 600-second propagation and restricted witnessed evidence; otherwise emergency incident | NOT RUN |
| Probe cleanup | Advance authorized exact VM/boot cleanup or emergency containment, four negative reads for normal cleanup, independently verified original disk inventory, primary API ready | NOT RUN |
| Isolated R2/DB restoration | Generation marker and hashes, three DB/Temporal/app state schema and health, no production writers | NOT RUN |
| Leader fencing | Stale epoch denied, candidate pending denied, committed epoch only authorized | NOT RUN |
| Rollback and ambiguity | No duplicate dispatch; detach/commit timeouts reconciled; old leader properly fenced/restarted | NOT RUN |
| Paid fallback and expiry | Exact pricing/cap, cleanup independent of launch enabled but default-off, expired active leader preserved and never SOFTSTOPped, stopped retired candidate only after fenced handover | NOT RUN |
| Cloudflare monitoring | Real incident delivery, alert receipt, restore public health and no split-brain | NOT RUN |
| Recovery outcome | Time-stamped measured RTO/RPO, full cost ledger, no orphan resources | NOT RUN |

## 6. Independent critical review R2 closure matrix

| Rejected R1 finding | Required engineering evidence | State |
| --- | --- | --- |
| Expiry can terminate authoritative paid VM | PR #400, `tests/test_oci_paid_ttl_authority.py`, verified fail-closed case for RUNNING and active/pending/coordinator loss, exact-head checks and reviewer signoff | PENDING |
| Emergency cleanup authorization deferred until after launch | PR #401 normal and emergency exact-selector CLI tests; Phase 1 approval binds BOTH launch and containment; independent backup operator and hard cutoff | PENDING |
| PR #393 misrepresented | R2 distinguishes **merged scheduling decoupling** from **unverified termination safety**, keeps default-off `KATCHA_OCI_CLEANUP_APPROVED` | DOCUMENT REVISED |
| Post-revocation IAM access unproven | Same guest `verify-revoked`: 4 explicit denied reads after tag removal, max 600 s; failure initiates exact emergency containment and incident | PENDING |
| Test cleanup reports green without checking whole production | Both `original_disk_inventory_verified=false` and `production_api_readiness_verified=false` in runner; independent OCI inventory, primary health and coordinator checks | PENDING |
| Doc PR tests counted as operational proof | PR #391 doc-only checks **2 success/8 skipped**; targeted PR #400/#401 and accepted main CI, then live checks required | PENDING |
| Provider proof and resource envelope absent | Fresh OCI/FinOps quote, fifth ACTIVE existing bundle, witnesses and 50/100 GB actual headroom measured | PENDING |

**Decision stays NO-GO until each required row is evidenced, not merely linked to a proposed pull request.**

## 7. Approval handoff and retention

Third-party reviewers should annotate the **open draft PR**, link their restricted evidence attestations and request corrections. No reviewer can silently set a gate to PASS without verified provenance. The operator resolves requested changes on a branch and re-runs CI; reviewers approve the immutable resulting revision. **Do not merge a review PR as a substitute for cloud execution permission.**

When all prerequisites are met, the user should explicitly authorize the precise stage and reviewed commit by an affirmative message including the named resource envelope and cost ceiling. If approval changes, re-evaluate affected gates. Preserve sanitized logs and actual bill/cleanup outcomes for postmortem. Any incident retains evidence until independently reconciled, never blindly deleted to “restore the green state.”
