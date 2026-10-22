# Dedicated outbound acquisition worker: deployment gate

**Status: DESIGN AND READ-ONLY PREFLIGHT ONLY. No separate host deployed.**

This change adds a credential-free network diagnostic; it does not activate paid
cloud resources, add a proxy, change primary OCI networking, enable the production
poller, bypass YouTube checks, or grant content acquisition rights.

## Purpose and platform boundaries

A dedicated, continuously operating worker on a separately routed outbound
network is useful for isolating source discovery and licensed/authorized
transfers from the existing OCI A1 control plane. Network isolation does not
guarantee that YouTube accepts traffic. Do not implement rotating identities,
CAPTCHA avoidance, account cookie harvesting, fingerprint spoofing, or retries
intended to evade rate limits, access controls, or YouTube anti-bot checks.

Use Katcha's existing YouTube Data API and OAuth integration for discovery,
channel management, publishing and analytics. The Data API is not a general
method for downloading video file bytes. To acquire footage, prefer direct
uploads, files or signed links supplied by rights holders, licensed feeds,
operator-provided authorized assets, and original material. A YouTube URL
may be retained for editorial reference without acquiring its video bytes.
A discovery result is not proof of acquisition or publication permission.

## Proposed architecture

    Existing OCI A1: Katcha API / Temporal / AI / editorial policy
         |
         | Authenticated, scoped task lease / acknowledgment over HTTPS
         v
    Dedicated outbound host: isolated VCN or separate provider network
         |-- Official YouTube Data API: metadata discovery only
         |-- Approved media origins: rights-authorized downloads
         |-- Cloudflare R2: resumable multipart transfers
         |-- Heartbeats, structured failures, bounded work
         |
         v
    Katcha asset manifests, review, rights checks, private-first publishing

This is a topology proposal, NOT an implemented worker. Before connecting a
host, Katcha requires a remote-safe task lease/heartbeat/cancel protocol (or
secure Temporal worker connection), a narrowly scoped non-interactive service
principal, signed limited-duration R2 upload access, and idempotent result
recording. No public inbound listener, PostgreSQL port, Temporal port, or R2
admin credential should be exposed. Never reuse operator-ui credentials on
the acquisition host. Interactive OCI Bastion sessions are for administration,
not steady-state worker operation.

## Compute and cost gate

Per OCI Always Free documentation checked October 18, 2026, Ampere A1's
free allowance is 2 OCPUs / 12 GB total. Katcha's existing OCI primary already
uses 2 OCPUs / 12 GB; there is no unallocated free A1 headroom to assume.
OCI also advertises E2.1.Micro free-tier instances (1/8 OCPU, 1 GB RAM each),
subject to actual tenancy limits, stock, boot volumes, and region. Such a
micro VM may suffice for metadata / bounded transfers, but should not run
4K video processing. An alternative VPS requires a verified recurring-price,
transfer allowance, hard spend cap, and dedicated egress. A different cloud
IP or provider still cannot promise YouTube challenge avoidance.

Before creating resources:

1. Inventory the live OCI tenancy: compute/OCPU/RAM, all boot and block volumes,
   VCN count, subnets, route tables, public IPs, NSGs, tenancy limits and region
   capacity. Verify the primary OUTBOUND public IP, not private 10.42.2.144.
2. Verify billable components, NAT charges, resource quotas and a strict cap.
   Do not resize, delete or reconfigure katcha-prod-primary-001 or any data volume.
   Keep recovery, external compute, and paid fallback SAFE_FALSE.
3. Use a separate VCN/non-overlapping subnet or a separately contracted
   provider network. Prefer no inbound public reachability, a private instance
   with controlled NAT, default-deny NSGs, and minimal outbound DNS/HTTPS/NTP.
   Verify free-tier VCN count and NAT billing before use.
4. Run a persistent restricted systemd service with resource/disk limits,
   bounded restarts, automatic health checks, update policy, and secrets from
   a managed source. No daily interactive login should be required.

## Worker implementation acceptance contract (NOT yet complete)

- Rights-qualified job: source ID, approved origin, rights evidence, size cap,
  correlation ID, and clear failure classification.
- Egress policy: deny internal and cloud instance-metadata IPs, DNS rebinding
  and redirect-based SSRF; no unrestricted forwarding proxy.
- Durable work: short task leases, heartbeats, bounded exponential backoff,
  idempotency keys and cancellation. Prevent duplicate transfer on crash.
- Media limits: bounded-memory streaming, at least 3 GiB signed/approved
  transfers, R2 multipart resume after reboot, SHA-256/ffprobe checks and
  immutable acquisition provenance before promoting an asset.
- Challenges: explicit non-retryable or review-needed statuses for YouTube
  authentication requests, 403/restrictions, rights blocks and anti-abuse
  responses; never attempt identity rotation to defeat a block.
- Diagnostics: provider status, Retry-After, stage, worker identity, lease,
  trace ID, transfer bytes, disk pressure, R2 state, queue lag, and redact
  credentials, cookies and signed URLs.
- Operations: safe kill switch; no dependency of existing production, Katcha
  AI, or publishing on this worker; private-first uploads remain unchanged.

## Credential-free outbound diagnostic (IMPLEMENTED)

On the primary machine with the script deployed, run:

    python3 scripts/katcha_egress_readiness.py

Record the observed public egress_ip. Status CONNECTIVITY_ONLY never claims
independent routing. On the second host, with the recorded primary IPv4:

    python3 scripts/katcha_egress_readiness.py --primary-egress-ip 'PRIMARY_PUBLIC_IPV4'

This script only calls an external public IPv4 reflection service and Google's
public YouTube Data API discovery document over TLS. It does not log in,
download video, test restricted URLs, or probe bot-control eligibility.

DISTINCT_EGRESS_VERIFIED means only that observed public IPv4 egress differs
from the provided primary IPv4 and the API discovery endpoint responded.
BLOCKED exits nonzero. It DOES NOT prove a separate VCN, independent ASN,
24/7 service readiness, R2/queue integration, media download authorization,
or ability to avoid YouTube checks. Independently verify provider route tables,
access policies, deployment health and worker-to-R2 end-to-end transfer.

## Rollout and rollback gates

1. Approve host based on live tenancy inventory, network plan and hard cost
   ceiling. No financial or production changes without a reviewed plan.
2. Implement and test the missing remote-safe API/lease/auth/asset pipeline,
   transfer limits, redaction, rights and failure behavior.
3. Provision with reviewed infrastructure-as-code. Confirm outbound identity,
   network segmentation and no inbound listeners; keep worker disabled.
4. Prove synthetic/authorized-media tests including 3 GiB upload, restart,
   deduplication, rights denial, disk exhaustion and queue outage.
5. Enable low-concurrency approved work; observe 24/7 heartbeats and costs.
   If it fails, quarantine the worker queue and preserve active R2 state;
   do not change production DB, networking or publishing behavior.

## Current blockers

- Live OCI quota/instance/VCN/billing inspection is not available to this PR.
- Remote job protocol, scoped service principal and secure transfer plumbing
  are not implemented by this preflight.
- Media rights and YouTube access restrictions remain independent of egress.

References:
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- https://developers.google.com/youtube/v3/
- https://www.youtube.com/t/terms
- docs/INGESTION.md
- docs/YOUTUBE.md
- docs/PRODUCTION_DEPLOYMENT_RUNBOOK.md
