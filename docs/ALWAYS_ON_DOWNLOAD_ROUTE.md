# Always-on anonymous media download route

The OCI application host currently receives YouTube bot challenges for the
incident clips. The same public clip downloaded without cookies from the
operator network. A live isolated container on OCI recovered through a temporary
SSH relay and produced the identical file hash. This is acquisition evidence,
not an installed permanent route, scene verification, or a reuse license.

## What is implemented

The ingestion image contains OpenSSH, Node and yt-dlp/EJS. When explicitly enabled,
the existing ingestion worker manages an SSH SOCKS listener on **127.0.0.1:18081**
inside its own container. It forwards through a dedicated non-root account on the
approved download host. There is no public proxy port, new application service,
new database, browser-cookie export, or extra cloud identity.

A lost SSH session restarts with 1–60 second backoff. Keepalives detect an
unresponsive session. The helper stops after graceful worker shutdown. SSH uses
only a dedicated key and an independently verified host key; it never accepts
unknown host keys automatically. Public downloads remain the first attempt, with
one anonymous fallback after the recognized YouTube challenge. Account cookies
are not sent through the route. A failed route is reported, not called success.

## Host admission before activation

Use an authorized always-on Linux machine whose outbound network actually passes
a complete YouTube download. A second datacenter VM is not proof of usable access.
Do not activate the route based only on SSH reachability or a SOCKS healthcheck.

The host needs OpenSSH, outbound DNS/HTTPS, a dedicated `katcha-download` account,
and sufficient bandwidth. It does not receive application databases, cloud/API
credentials, media-store credentials, or production authorization. Use a dedicated
key. Restrict its SSH account to local TCP forwarding: no interactive sessions,
TTY, agent forwarding, X11 forwarding, remote forwarding or tunnels. Configure
`AllowTcpForwarding local`, `AllowStreamLocalForwarding no`, `GatewayPorts no`,
`PermitTunnel no`, `PermitTTY no`, `X11Forwarding no`, and `ForceCommand /bin/false`
in a `Match User katcha-download` block. Validate with `sshd -t` before reloading.
The authorized key should use `restrict,port-forwarding`. Restrict inbound SSH to
the production host and separately approved administration addresses. Keep the
host isolated from sensitive internal networks and deny private/metadata-network
forward destinations with its firewall. These host controls need verification
on the selected host before acceptance.

Obtain its SSH host key through the provider console or another trusted channel;
`ssh-keyscan` alone is not identity verification. Store it under the alias
`katcha-download-route` in the known-hosts file. Keep the dedicated private key
outside Git. On the application host, place it in a root-protected directory,
owned by UID 10001 and mode 0400 so the unprivileged worker can read its bind mount.
The known-hosts file must be readable by UID 10001. No secret contents belong in
logs or acceptance reports.

## Application configuration (disabled until host acceptance)

Set the following in the protected production environment only during an approved
configuration change, substituting the verified host and local secret-file paths:

```dotenv
KATCHA_DOWNLOAD_ROUTE_ENABLED=true
KATCHA_DOWNLOAD_ROUTE_HOST=download.example.com
KATCHA_DOWNLOAD_ROUTE_USER=katcha-download
KATCHA_DOWNLOAD_ROUTE_PORT=22
KATCHA_DOWNLOAD_ROUTE_KEY_FILE=/etc/katcha/download-route/id_ed25519
KATCHA_DOWNLOAD_ROUTE_KNOWN_HOSTS_FILE=/etc/katcha/download-route/known_hosts
```

The supervisor selects `deploy/docker-compose.download-route.yml` only for an
explicit true flag. The overlay mounts only the dedicated SSH files and sets the
worker's proxy address to its loopback listener. Missing or malformed settings
fail validation. With no flag, ordinary production startup is unchanged. This
configuration must use a released image containing the helper and the matching
supervisor source; use the existing supervised deployment process and its
migration/backup/workflow checks. Do not start a competing Compose supervisor.
The production service count remains unchanged.

After activation, `python -m katcha.ops.download_route --healthcheck` inside the
worker validates SOCKS negotiation only. Perform a complete approved clip
acquisition and verify `youtube_access_mode=approved_route`, matching hash,
ffprobe metadata, storage receipt and channel-scoped Clip Library linkage. Verify
reconnection after an intentional temporary route interruption and shutdown of
the helper when the worker stops. Do not declare E2E based on the healthcheck.

To disable, set `KATCHA_DOWNLOAD_ROUTE_ENABLED=false` and apply the change through
the same supervisor/deployment process. Direct access remains available; the
optional cookie fallback remains governed by its existing configuration.

## Current infrastructure finding and proposed next decision

Read-only inventory on 2026-10-17 found one OCI instance: the existing production
primary. No spare running download host was found there. Two E2 Micro allowance
slots and 50 GB of boot/block allowance remain; an ordinary 50 GB boot disk would
consume that remaining storage. Shape listing does not prove physical capacity
or billing eligibility. AWS inventory could not be completed because its login
session had expired. No compute was created and no IAM, recovery or production
service settings were changed by this investigation.

A possible **separately approved admission test** is one home-region
`VM.Standard.E2.1.Micro` with one 50 GB boot disk, no paid fallback, no data volumes,
no Vault/instance-principal access, and no production/recovery identity. Before
launch, refresh quota/billing/image/network evidence and review exact resources,
SSH allowlists, bounded test lifetime and failed-test cleanup. Accept it only if
the complete cookie-free clip download succeeds from its network. If YouTube also
challenges it, stop: do not rotate through new addresses or call it ready.
Oracle documents the two E2 slots and shared 200 GB storage allowance at
https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm.

Automatic scene/action/episode verification remains separate work. The current
goal completion guard still requires review for scene-specific retrieval; this
network helper does not remove that evidence requirement.
