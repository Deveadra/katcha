# Remote acquisition worker and verified handoff

The acquisition host downloads approved media; the primary Katcha worker verifies
and imports it. The host polls Katcha over HTTPS and uploads directly to the existing
S3-compatible object store using individual short-lived multipart grants. It needs
no inbound port, database, Temporal or object-store credentials. This implementation
does not provision a host or activate a new paid provider.

## Setup

1. Deploy the API and main ingestion worker from the same release, with migrations
   through `0066_acquisition_handoff`. Set `KATCHA_REMOTE_ACQUISITION_ENABLED=true`
   on both the API and main ingestion worker. Keep it false until the selected host/route is admitted.
   For a supervised test, set `KATCHA_REMOTE_ACQUISITION_AUTO_DISPATCH_ENABLED=false`
   on both services: only explicitly queued jobs use the remote host, while unrelated
   ingests retain their existing route. Linked jobs still wait for verified handoff.
   The automatic dispatch setting defaults to true for normal operation.
2. Configure a dedicated control principal with exactly `acquisition:work` and
   explicit channel UUIDs. Never give the host the operator token, wildcard channels,
   extra scopes, production environment file, or bucket credentials. Use one principal
   per host so execution ownership and revocation remain meaningful.
3. Install the same release with Python 3.12, FFmpeg and Node 22 on the acquisition
   host. Use a writable private state volume with space for the job byte limit (twice
   that limit for extractor/merge work), plus a 256 MiB reserve. Set the maximum
   job size to match the real host disk capacity. The default is 4 GiB; the protocol
   supports up to 64 GiB with 8 MiB parts and widened database byte counters.
4. Copy `deploy/acquisition/worker.env.example` to
   `/etc/katcha-acquisition/worker.env`, fill the HTTPS API origin and channel UUIDs,
   and install the dedicated token in `/etc/katcha-acquisition/worker-token` mode
   0600, root owned. Install the supplied systemd unit. systemd passes the token as
   a credential into the unprivileged service; restart/reboot reuses its state volume.
   The service has no provisioned VM dependency and can first be exercised on an
   existing authorized machine.
5. For a manual run, set `KATCHA_ACQUISITION_TOKEN_FILE` to a private token file,
   `KATCHA_ACQUISITION_STATE_DIR` to a private persistent directory and the other
   variables from the example. Run `python -m katcha.acquisition.worker --once` for
   one queue pass, or omit `--once` for the persistent worker. Do not use `--once`
   as a daemon health check: it deliberately catches failures and retains recovery
   state. Inspect job status and service logs for actual acceptance.

If the HTTPS API is behind Cloudflare Access, configure a dedicated Access service
credential as well, or use an authorized private HTTPS API origin. Optional
`KATCHA_ACQUISITION_CF_ACCESS_ID_FILE` and `KATCHA_ACQUISITION_CF_ACCESS_SECRET_FILE`
point to private credential files (systemd LoadCredential drop-ins are suitable).
Both are required together. These headers stay on control requests and never reach
source or storage requests. The API still authenticates the narrow worker principal;
a broad interactive operator/Access session cannot replace it. For a private HTTPS
origin with an internal CA, set `KATCHA_ACQUISITION_CONTROL_CA_FILE` to its CA bundle.
Certificate and hostname verification remain mandatory; this CA applies only to
control requests, never to source or storage connections.

The primary supervisor scans persisted jobs and starts deterministic Temporal
workflows. A failed dispatch is repaired on the next pass or restart. The worker
process uses a host lock to prevent two processes from executing the same journal.
Default execution is sequential, suitable for a small acquisition host.

## Dispatch and handoff

Use an existing authorized `AcquisitionReceipt` created with a real, unexpired
`ProviderPermissionV1`, an explicit provider, approved HTTPS hosts and channel-owned
candidate. Queue it with:

```http
POST /v1/channels/{channel}/acquisition-jobs
{"receipt_id":"...", "max_bytes":4294967296, "media_type":"video"}
```

The operator's enqueue reserves the receipt and binds a managed source to the
same channel. Repeating the same receipt and settings returns the same job;
conflicting byte/type limits, source ownership or prior media cannot be overwritten.
The worker durably saves its claim before requesting execution, then heartbeats
under its authenticated principal and fenced claim/generation.

Direct public HTTPS media uses the approved pinned-DNS transport. Strong ETag plus
exact Content-Range permits continuation of a partial file; a changed representation,
ignored range, or missing resume evidence preserves uncertainty instead of silently
redownloading. Supported site URLs use yt-dlp with native HTTPS formats and local
FFmpeg merging. Every extractor HTTP request goes through the same approved
transport: CDN/API hosts also need explicit permission, redirects stay checked,
private addresses and ambient proxies are rejected, and credential-bearing URLs
never enter ordinary job views or logs. An allowlist can therefore cause a safe
refusal until all genuinely required provider hosts are authorized.

After acquisition the worker freezes the local file SHA-256/size, obtains a durable
multipart cursor, and uploads bounded parts. The server chooses the staging key,
freezes each part checksum/length, and signs only that part with a maximum 60-second
expiry. S3 checks Content-MD5. The server obtains ETags from S3 rather than trusting
a worker-supplied completion manifest. It reconciles a lost completion response
against the exact server-owned staging object before attempting completion again.
The host never receives a grant to list a bucket, read another job, or write a
canonical object.

The remote report means **awaiting validation**, never successful acquisition.
The primary worker independently reads and hashes stored bytes, checks their exact
size, probes media type/duration/dimensions and decodes an AV frame. It promotes
through a managed server-side multipart copy to `media/sha256/{sha}` and verifies
those canonical bytes before one transaction records the successful receipt,
validation, channel-linked SourceItem/Clip and completed job. A media download does
not change any rights assessment or grant production clearance. Temporary staging
bytes are removed only after the accepted transaction commits.

When remote acquisition is enabled, new Clip ingest and Editorial requests dispatch
an already-authorized channel/candidate receipt into the remote queue automatically.
Missing permission blocks with an actionable error; the primary never silently
falls back to a local download. `KATCHA_REMOTE_ACQUISITION_MAX_BYTES` bounds automatic
jobs to the host's configured capacity (default 4 GiB).

Existing Clip ingest and Editorial workflows wait durably for linked remote jobs
before running their normal processing steps. Direct invocation of the old ingest
activity also refuses to download a pending remote job locally. Standalone queueing
acquires media for review; it does not authorize new AI-provider spending.

## Recovery and operator controls

- The worker journal, claim identity, source representation and multipart cursor
  survive process restarts. Acquired local bytes are rehashed before resuming.
  A known completed download can renew its original expired upload lease while
  the job remains leased. A matching server-side multipart cursor can also recover
  from a `worker_lost` quarantine, with a new generation fencing the previous
  process; this path never contacts the source provider.
- Lost source execution without a resumable representation is retained as unknown.
  After the lease has been expired for five minutes the primary supervisor
  quarantines it as `recovery_required`. It never assigns another worker to replay it.
  An unknown provider execution without a frozen upload requires operator review.
  `POST .../{job}/release-source` with `acknowledge_unknown_outcome: true` explicitly
  releases that stopped source for a new authorized receipt/operation. The old
  receipt stays unknown; this control never starts a download or invents success.
  Cancellation or quarantine fences subsequent upload completion and promotion.
- An interruption during yt-dlp extraction is `session_lost`; it is not automatically
  replayed after restart. Authentication challenges are classified as
  `authentication_required`. This worker/handoff does not claim that an operator
  cookie file proves a browser CAPTCHA was solved. Secured interactive browser and
  Telegram challenge control remain a separate capability.
- Read list/detail status through the existing channel-scoped acquisition API.
  Status, phase, byte progress, heartbeat, failure code and source linkage are real
  persisted state. Detailed URLs and signed grants remain private lease responses.
- Operators can cancel a job, reconcile a lost lease, retry independent validation
  with `POST .../{job}/validate`, or clean stopped staging uploads with
  `POST .../{job}/upload/cleanup`. Workers cannot use these operator actions.
  Permission and channel ownership are rechecked before accepting media, including
  after a long validation operation. Revocation cannot be bypassed by a late upload.
- Preserve unknown local artifacts for operator reconciliation. Known completed
  media is removed from the host after the server acknowledges the handoff. Keep
  the state volume private; keep optional operator cookies in a separate private
  file. Never put cookies or bearer tokens into a job payload or Telegram message.
- Configure object-store lifecycle abort of abandoned **incomplete multipart
  uploads**. Accepted media is outside the staging prefix. Stopped staging objects
  can be cleaned through the authenticated API; canonical objects require the
  ordinary asset lifecycle rules, not a blanket acquisition cleanup.

Setting the feature flag false stops new claims, uploads and heartbeats. Read,
cancel, reconcile and independent validation remain available for a safe drain.
Do not resize production or provision a new route as part of installing software.
A separate VM behind the same NAT is still the same outward identity; admission
requires a measured independent route and a released-worker storage/channel test.

For a persistent workstation with direct Cloudflare Access authentication, see
[Persistent workstation acquisition](ACQUISITION_WORKSTATION.md).

## Running acquisition on the core VM

The existing worker can run on VM1 using the same scoped HTTPS API and durable
journal. See [Single-host operation](SINGLE_HOST_OPERATION.md) for the shared
resource queue, Compose profile, rollout gates, and Forescene acceptance checklist.
The worker protocol and provider permission checks stay the same on either host.

## Acceptance evidence

Offline tests exercise the real worker/control API/receipt promotion with synthetic
provider and S3 transports, actual image decoding, native media probes, interrupted
multipart completion, restart recovery, oversized/mismatched media, expired leases,
cancellation and channel/worker isolation. Metadata boundary tests exercise 3 GiB+
part offsets without transferring 3 GiB. These are repeatable software checks;
they are not evidence of live YouTube success, a deployed independent host, a real
3 GiB network transfer, browser verification, or sustained host capacity.

Provider compatibility reference: [Cloudflare R2 S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
confirms UploadPart Content-MD5 and multipart copy support. The offline signature
check also verifies that actual boto3 grants sign both Content-Length and Content-MD5.
Live provider acceptance remains a separate deployment check.
