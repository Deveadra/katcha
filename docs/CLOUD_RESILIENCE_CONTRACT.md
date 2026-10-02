# Katcha cloud resilience contract

Status: architecture contract adopted October 2, 2026.

This contract amends the hosted deployment plan before production infrastructure is provisioned. It defines the non-negotiable behavior of the cloud runtime; individual provider implementations may change without weakening these properties.

## Availability model

OCI Ampere A1 Always Free is the preferred steady-state control plane because it minimizes burn. It is **not** treated as guaranteed capacity.

Recovery order:

1. restore or replace the healthy Always Free A1 instance when capacity exists,
2. retry a valid placement within the tenancy/home-region options allowed by the account,
3. if free capacity is still unavailable and the emergency-spend policy permits it, create the smallest approved paid OCI compute shape,
4. restore the exact durable state, acquire leadership, and resume only reconciled work,
5. periodically probe for acceptable Always Free A1 capacity,
6. migrate authority back only after restore/health/fencing acceptance,
7. destroy paid fallback compute automatically after the configured grace period.

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

A minimal encrypted break-glass bundle is also kept outside OCI. It contains only the material needed to bootstrap a replacement environment, for example:

- R2 restore credentials or a recoverable path to them,
- Cloudflare recovery-coordinator deployment/configuration material,
- encrypted Katcha credential-encryption key recovery material,
- source-control/deployment bootstrap instructions and identities,
- AWS rendering bootstrap material where it cannot be derived elsewhere.

The bundle is not mounted into normal Katcha processes and is not readable by the normal OCI runtime identity.

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

Cloudflare Tunnel is the origin path. The operator/admin surface is protected with Cloudflare Access. WAF/rate-limit rules are applied by endpoint class.

Public/machine endpoints are explicitly classified rather than globally bypassing Access. Examples that require separate policy include:

- OAuth redirect/callback endpoints,
- verified signed webhooks,
- health probes that intentionally reveal only minimal state,
- machine-to-machine control endpoints with their own strong authentication.

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
5. restore PostgreSQL/Temporal from R2 with the normal OCI instance unavailable,
6. bootstrap using the off-OCI break-glass path without depending on OCI Vault,
7. exceed an external-compute budget in a fixture environment and prove no additional paid jobs launch,
8. verify the operator surface is inaccessible without Access while OAuth/machine endpoints retain only their intended access path,
9. run the full local-PC-off acceptance test while the hosted control plane owns leadership.

The normal target remains near-zero fixed infrastructure cost. Reliability is preserved by allowing a small, explicit, observable, automatically-expiring amount of emergency spend rather than accepting an unbounded outage.
