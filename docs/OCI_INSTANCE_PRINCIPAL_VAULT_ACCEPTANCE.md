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

An approved operator must then make the scoped defined-tag change, verify it
applies only to the isolated candidate, and supply the OCID of a known-existing
out-of-allowlist secret for the negative test. Then, on the same guest:

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

## Cleanup and stop conditions

Any mismatch, unexpected grant, timeout, missing guest identity, missing
fifth-secret fixture, primary health failure, or exhausted capacity blocks
acceptance. Never auto-tag, launch a second instance, change production
leadership, or retry a timed-out launch blindly.

Cleanup must follow `scripts/oci-isolated-identity-probe.py cleanup` with
its separate scoped approval and exact probe ID; verify termination of **the
probe VM and its newly created boot volume**, complete release of 50 GB, no
data-volume attachment, and unchanged production health and safety switches.
A failed or ambiguous cleanup is an operator incident, not a successful test.

Even successful access tests do **not** prove guest bootstrap, R2 access,
PostgreSQL restore, fencing, Cloudflare authority, or failover. Those need
separate controlled recovery-game-day acceptance.
