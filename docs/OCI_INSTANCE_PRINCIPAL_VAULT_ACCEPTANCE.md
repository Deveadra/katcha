# OCI candidate instance-principal Vault acceptance (offline-prepared)

**Status:** guest-side probe and mock regression tests only. No instance launched,
no defined tag changed, no Vault bundle retrieved. Live approval remains required.
The existing isolation-only launch plan is in
[OCI_ISOLATED_CANDIDATE_PROBE.md](OCI_ISOLATED_CANDIDATE_PROBE.md).

## Exact evidence boundaries

The existing operator read-only metadata audit establishes the dynamic group and
four exact IAM grants. It cannot prove the enforcement decision seen by an OCI
instance principal. The guest-side tool
`scripts/oci-candidate-vault-principal-probe.py` tests OCI's own data plane
using `--auth instance_principal`, with no API-user key or administrator session.
The tool selects only `data."secret-id"` on an authorized read; it never prints
or writes the bundle bytes, token, ID, or OCI stderr.

The two guest-side stages MUST use the *same* individually approved E2 Micro
identity-probe instance. They are not generic production-runbook commands:

1. **Untagged negative stage:** with no `KatchaRecovery.Candidate` defined
   tag, `deny-four` must obtain an explicit OCI authorization denial for
   each of the four known-active secret IDs. Network errors or transient OCI
   failures are **not** counted as denials.
2. **Tagged positive stage:** after *separate approval* and operator-controlled
   application of the exact candidate defined tag, `allow-four` must retrieve
   four matching secret IDs via instance principal, then verify denial for a
   **known-existing** fifth secret that is outside the policy allowlist. An
   invented or nonexistent OCID does not establish least privilege. If no safe,
   existing fifth secret is available, record **negative scope unverified**
   rather than creating one or claiming acceptance.

In addition to these tests, independently re-read the dynamic-group matching
rule and the four `target.secret.id` policy grants after tagging. No broad Vault
`manage`, cross-compartment privileges, or tags on the production primary are
permitted.

## Offline preparation and tests

```bash
cd ~/src/katcha
python3 -m py_compile scripts/oci-candidate-vault-principal-probe.py
python3 -m pytest -q tests/test_oci_candidate_vault_principal_probe.py
```

A passing result is **offline-tested only**.

## After explicit approval and launch

The **approval record** must identify instance OCID, image, private subnet,
NSG, 50 GB boot volume, boot minimum, current region-wide storage and compute
inventory, account billing model, timing, allowable OCI API Vault reads,
the proposed tag transition, and cleanup authorization. The existing isolated
launch runner creates only an untagged VM; it does not read secret values.

Install Python 3 and OCI CLI on the isolated candidate through a reviewed,
approved procedure. Provision the four *secret OCIDs only* as the environment
variables `KATCHA_OCI_PRODUCTION_ENV_SECRET_ID`,
`KATCHA_OCI_AWS_BUNDLE_SECRET_ID`,
`KATCHA_OCI_BACKUP_ENV_SECRET_ID`, and
`KATCHA_OCI_RESTORE_ENV_SECRET_ID`. Do not copy secret bodies or GitHub
recovery/API credentials onto the guest.

On the isolated guest, using the **actual approved candidate OCID**, execute:

```bash
python3 scripts/oci-candidate-vault-principal-probe.py deny-four \
  --expected-instance-id "$APPROVED_CANDIDATE_INSTANCE_ID" \
  --approve-live-vault-reads
```

Only after the four *untagged* denials have been individually observed,
an authorized operator may perform the one-time candidate-tag transition
using the OCI administrator's isolated session from the repository root:

```bash
.venv/bin/python scripts/oci-isolated-identity-probe.py tag-candidate \
  --instance-id "$APPROVED_CANDIDATE_INSTANCE_ID" \
  --acknowledge-four-untagged-vault-denials \
  --approve-isolated-candidate-iam-tag
```

This command is an **OCI IAM-affecting resource mutation** and requires its
own approval; the flags do not themselves grant permission. It refuses to
tag a production instance, any non-probe VM, an unexpected network/disk
configuration, a candidate already tagged, or a probe without a running
production primary. It preserves existing defined-tag namespaces, modifies
only `KatchaRecovery.Candidate`, and re-verifies the exact probe. The
operator must independently confirm the current dynamic-group scope and
four exact policy grants. OCI authorization propagation may take time;
a transient denial is **not** a successful positive test.

Supply a known-existing fifth secret outside the grant list for the
negative test. Then, on the same guest:

```bash
python3 scripts/oci-candidate-vault-principal-probe.py allow-four \
  --expected-instance-id "$APPROVED_CANDIDATE_INSTANCE_ID" \
  --forbidden-secret-id "$KNOWN_EXISTING_FORBIDDEN_SECRET_ID" \
  --approve-live-vault-reads
```

Capture sanitized CLI JSON results, approved OCI resource identifiers in a
restricted evidence record, observed timestamps, IAM policy version, and OCI
monitoring/Audit events (without secret contents). Do not place IDs, raw stdout,
credentials or Vault response bodies in public PR logs.

## Cleanup, IAM revocation and preauthorized emergency containment

**Approval timing:** the exact-probe `cleanup` **and**
`emergency-contain` paths, operator identity, exact resource selector,
limited lifespan, billing/incident escalation and fallback administration
must all be reviewed and preauthorized **before Phase 1 launch**.
A later approval to use IAM tags may remain separate; containment permission
cannot be deferred until after creating a potentially compromised VM.

After the positive tagged tests, revoke **only** the candidate defined tag
using the OCI administrator session and the same isolated candidate:

```bash
.venv/bin/python scripts/oci-isolated-identity-probe.py revoke-tag \
  --instance-id "$APPROVED_CANDIDATE_INSTANCE_ID" \
  --approve-isolated-candidate-tag-revocation
```

This returns `CANDIDATE_TAG_REMOVED_DATA_PLANE_NOT_YET_VERIFIED` and
**does not terminate the guest**. Tag removal is not proof effective
authorization expired. Still on the guest run:

```bash
python3 scripts/oci-candidate-vault-principal-probe.py verify-revoked \
  --expected-instance-id "$APPROVED_CANDIDATE_INSTANCE_ID" \
  --max-propagation-seconds 600 \
  --approve-live-vault-reads
```

The guest polls for at most 600 seconds, emits success only after four explicit
denials and fails on transport/IAM errors or continued successful reads. Verify
the tag's absence in OCI metadata separately. Capture sanitized evidence
with timestamps; do not use the original **untagged** deny-four evidence as
proof of post-revocation denial. **If any read remains allowed, raise a
security incident; do not claim revocation successful.**

Only after the reviewer witnesses all four post-revocation denials, invoke
preapproved normal cleanup from the trusted administrator workstation:

```bash
.venv/bin/python scripts/oci-isolated-identity-probe.py cleanup \
  --instance-id "$APPROVED_CANDIDATE_INSTANCE_ID" \
  --acknowledge-post-revocation-four-denials \
  --approve-cleanup-of-test-instance-and-boot-volume
```

The acknowledge flag records operator attestation; it does **not** itself
query Vault or prove that denials occurred. The restricted evidence bundle must
retain the guest result. Normal cleanup refuses a candidate still tagged.

**Emergency fallback (authorized before launch):** if revocation or guest
access fails, treat the probe as potentially privileged, isolate the incident
and execute the separate, verified exact-resource emergency containment action:

```bash
.venv/bin/python scripts/oci-isolated-identity-probe.py emergency-contain \
  --instance-id "$APPROVED_CANDIDATE_INSTANCE_ID" \
  --expected-boot-volume-id "$APPROVED_NEW_PROBE_BOOT_VOLUME_ID" \
  --approve-emergency-probe-containment
```

This does **not** broaden secret IAM, touch production, or retry an unknown VM.
The runner checks probe identity, exact boot volume, no production data disk,
private VNIC/NSG and unique resource tags before exact VM termination and
new-boot-volume cleanup. It bypasses the failed tag-revocation precondition
**only** within that explicitly preauthorized emergency resource envelope.
Output is `EMERGENCY_TEST_RESOURCES_REMOVED_IAM_UNVERIFIED`; OCI termination
does not prove removal of issued tokens or effective policy revocation.
If even this action cannot identify or terminate the VM, escalate immediately
to an independently authorized OCI administrator for direct containment;
leave production untouched and preserve forensic evidence.

**Independent post-cleanup verification is mandatory**, because the runner
checks instance metadata and new boot removal but does **not** verify production
API readiness or prove that the entire preexisting volume inventory remained
unchanged. Read back exact termination, boot volume absence, all previous
disk OCIDs/counts and occupied GB, running production primary, public health,
Cloudflare active epoch, all disabled recovery/spend flags, and account costs.
Record these as independent evidence; any mismatch means FAIL.

The operator's two-hour runtime is **not** a mechanically enforced expiry:
an independent reliable cutoff and backup operator are still a blocking
preflight requirement. No resource launch is authorized solely by the CLI
existence or a timestamped checklist. A failed or ambiguous cleanup is an
open security/cost incident, not a completed acceptance test.

Even successful access tests do **not** prove guest bootstrap, R2 access,
PostgreSQL restore, fencing, Cloudflare authority, or failover. Those need
separate controlled recovery-game-day acceptance.
