# Katcha cloud resilience contract

Status: architecture contract adopted October 2, 2026.

This contract amends the hosted deployment plan before production infrastructure is provisioned. It defines the non-negotiable behavior of the cloud runtime; individual provider implementations may change without weakening these properties.

## Availability model

OCI Ampere A1 Always Free is the preferred steady-state control plane because it minimizes burn. It is **not** treated as guaranteed capacity.

Recovery order:

1. replace the current same-AD Always Free A1 instance and reattach its durable data volume when that AD remains usable,
2. if same-AD recovery fails, try every configured alternate-AD Always Free A1 target and restore the newest acceptable immutable R2 recovery point onto a fresh durable volume in that AD,
3. only after all configured free A1 placements fail, and only when the emergency-spend policy permits it, try the smallest approved paid OCI compute shape in the current AD and then alternate ADs,
4. acquire leadership only after storage restore, local readiness, public-route readiness, and fencing acceptance,
5. periodically probe for acceptable Always Free A1 capacity,
6. migrate authority back only after restore/health/fencing acceptance,
7. destroy paid fallback compute automatically after the configured grace period,
8. retain superseded durable volumes for a bounded rollback window, then delete only those explicitly retirement-tagged, unattached, and proven not to be the active leader's volume.

Paid fallback is bounded by both a maximum lifetime and a maximum infrastructure spend. It is a continuity mechanism, not the normal runtime.

## Recovery coordination

Cloudflare Workers KV must not be used as the recovery mutex/lease.

The recovery coordinator uses one SQLite-backed Durable Object as the strongly consistent authority for:

- current deployment epoch,
- current leader identity,
- lease owner and expiry,
- recovery generation,
- most recent healthy deployment,
- paid-fallback state and expiry,
- last completed recovery action.

GitHub recovery workflows also use a single concurrency group. GitHub concurrency is defense in depth; the Durable Object is the cross-provider authority.

Every recovery action is idempotent against the recovery generation. A retry may inspect or continue the same generation but must not create a second leader.

## Failover fencing

Every control-plane deployment has a monotonically increasing integer deployment epoch.

Before a process may perform an externally visible mutation, its epoch must equal the coordinator's active epoch. This applies at minimum to:

- YouTube upload, publication, metadata, thumbnail, and scheduling mutations,
- provider-side job creation,
- paid AI/TTS calls above the configured free/approved route,
- render/analysis job dispatch,
- destructive or overwrite operations in object storage,
- operator notifications where duplicate delivery is material.

A replacement control plane increments the epoch only after durable state has been restored and validated. A stale Oracle VM that later wakes up cannot regain authority merely because its local services started.

## Backup immutability and credentials

R2 is used for off-VM recovery data, but runtime media credentials and backup credentials are separate.

Required credential model:

- runtime-media principal: only the media operations Katcha needs,
- backup-writer principal: create/write backup objects but no normal media administration,
- restore principal: read recovery objects and used only by controlled restore/recovery automation,
- bucket-configuration principal: manages retention/lock policy and is never available to Katcha runtime containers.

Database/Temporal backup objects are written under a dedicated locked prefix or dedicated bucket with retention rules that prevent overwrite/deletion for the required recovery window.

A restore test from an R2 recovery point is a production gate.

## Break-glass secrets

OCI Vault remains the normal secret authority for the OCI deployment.

A durable encrypted escrow is also kept outside OCI in a dedicated Cloudflare R2 bucket. The escrow contains the production environment, dedicated backup/restore environments, and AWS bootstrap material needed to reconstruct the control plane. Its Fernet decryption key is held outside OCI by GitHub Actions plus an operator-controlled offline/password-manager copy.

Normal Katcha runtime identities cannot read the escrow. Automatic recovery continues to use OCI Vault. Break-glass recovery is a manual workflow-dispatch choice only.

A break-glass recovery does not hand the durable escrow key to the replacement VM. GitHub reads and verifies the durable escrow, re-encrypts it with a fresh one-time key, uploads the ciphertext beneath a separate short-lived handoff prefix, and supplies only that handoff URL/key to the candidate. The handoff is deleted in an always-run cleanup step and is also covered by a lifecycle rule that removes stale handoffs within 24 hours. The durable escrow prefix is excluded from that lifecycle.

Escrow rotation is publish-then-switch: publish a new uniquely named encrypted object, update the GitHub pointer, pass the non-destructive escrow drill, and only then retire the superseded escrow. The durable escrow format is provider-neutral even though the current control-plane recovery backend still launches OCI compute.

See docs/BREAK_GLASS_RECOVERY.md for the setup, drill, rotation, and live recovery procedure.

## Cost circuit breakers

External compute and metered providers are fail-closed behind durable policy.

Before production cutover the implementation must enforce:

- global external-compute enable/disable switch,
- monthly infrastructure budget,
- per-provider monthly ceilings,
- maximum concurrently active external jobs,
- maximum attempts per logical job,
- maximum retry-spend allowance per logical job,
- paid-fallback maximum hourly/daily lifetime,
- circuit-open state after repeated provider failures,
- explicit operator-visible reason when work is deferred by budget policy.

No retry loop may create unbounded Fargate, Modal, Lambda, AI, TTS, or other metered jobs.

Startup credits, promotional credits, and free grants may reduce realized cost, but budget correctness must assume those credits do not exist.

## Edge access

The OCI host exposes no public application listener.

Cloudflare Tunnel is the only origin path. The FastAPI listener remains bound to loopback on the VM.

Cloudflare Access protects the entire human/operator hostname. Exact operator email addresses are the allowlist. More-specific Access applications define only two intentional exceptions:

- `/v1/*` is left to Katcha's own scoped bearer-principal authentication so durable machine clients and recovery automation do not depend on an interactive Access session,
- `/auth/callback` remains public for the ChatGPT OAuth redirect.

The Katcha application itself exposes only these unauthenticated control-plane paths:

- `/v1/health/live`,
- `/v1/health/ready`,
- `/v1/integrations/youtube/oauth/callback`,
- `/auth/callback`.

`/v1/health/workspace` requires Katcha control authentication. Any future public endpoint must be added explicitly to both the application and edge-policy contracts.

WAF custom rules are hostname-scoped and reject invalid methods on public health/OAuth endpoints plus common secret/admin probes. The Free-plan rate-limit budget is consumed by one IP-scoped rule covering only Katcha-specific public health and YouTube callback paths. The generic `/auth/callback` path is omitted from that free rate rule because Free rate-limit expressions do not expose the Host field.

Cloudflare ruleset automation changes only rules carrying Katcha-owned stable refs. It must not replace or delete unrelated zone rules.

## External heavy compute

The control plane does not perform heavyweight encoding or analysis merely because RAM happens to be available.

Current preferred routes:

- rendering: existing Remotion Lambda integration,
- burst/heavy analysis: provider adapter with Fargate Spot and Modal/equivalent backends,
- provider selection: policy-based on job requirements, current price/credits, availability, and budget ceiling.

The workload contract is provider-neutral: input artifact references and immutable job identity in; verified output artifact references and structured execution receipt out.

## Cutover gates added by this amendment

Production cannot be declared ready until all of the following pass:

1. simulate A1 unavailability and verify the paid escape hatch respects TTL/spend ceilings,
2. attempt two simultaneous recoveries and prove only one deployment epoch becomes leader,
3. wake a stale fenced control plane and prove it cannot publish or dispatch external work,
4. delete/overwrite a locked database backup using runtime credentials and prove the action is denied,
5. restore PostgreSQL/Temporal from R2 onto a fresh volume in another availability domain with the normal OCI instance and original data volume unavailable,
6. bootstrap using the off-OCI break-glass path without depending on OCI Vault,
7. exceed an external-compute budget in a fixture environment and prove no additional paid jobs launch,
8. verify the operator surface is inaccessible without Access while OAuth/machine endpoints retain only their intended access path,
9. run the full local-PC-off acceptance test while the hosted control plane owns leadership.

The normal target remains near-zero fixed infrastructure cost. Reliability is preserved by allowing a small, explicit, observable, automatically-expiring amount of emergency spend rather than accepting an unbounded outage.


## External compute spending circuit breaker

Paid compute authorization is not stored solely on the OCI host or Katcha
PostgreSQL. The external Cloudflare recovery coordinator owns a strongly
consistent budget ledger so a database/control-plane outage cannot bypass spend
limits.

Required production controls:

- global external-compute enabled/disabled switch, disabled by default,
- durable UTC monthly ceiling,
- explicit per-provider ceilings,
- maximum concurrent paid jobs,
- maximum settled + reserved spend for one retry group,
- idempotent reservation key before provider creation,
- settlement/release after provider outcome,
- provider-side TTL cleanup as a second independent guard.

OCI paid fallback uses both the existing local TTL/incident cap and the external
coordinator ledger. Future Modal, Fargate, Lambda, or other paid compute adapters
must reserve through the same coordinator before creating provider resources.
