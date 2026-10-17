# Clip retrieval diagnostics and acceptance

The incident conversation is `44e48cfa-9186-41d6-9890-e32ac6fe3bf6`.
The operator supplied preparation workflows
`command-source-prepare-b8ede92e-34c9-409a-ba5e-747ff7e22095` and
`command-source-prepare-b331d556-f0ee-4925-88b4-300c1a2311f2`, both referring
to ingestion `ingest-8c94f87960f1aae5956348901475d326`.

## Findings and changes

- Ingestion activity, child workflow and goal observation paths discarded nested
  failure causes. Attempt diagnostics now retain sanitized cause chains and the
  download, output, integrity, inspection, storage or persistence stage. Failed
  attempts record a domain event and source metadata before Temporal wraps the
  exception. Successful ingestion clears its prior failure diagnostic. Serialized
  Temporal application-error details preserve the stage across worker boundaries.
- `source_history` reads saved search configurations (`IngestionSource`). A media
  preparation receipt's historical `source_id` identifies a different model
  (`SourceItem`). Receipts now additionally name `source_item_id` and
  `candidate_id`; `source_item_status` reads observed media sources with a separate
  channel check. Legacy `source_id` stays for saved workflow compatibility.
- `workflow_status` previously returned only an execution state. Failed executions
  now include their sanitized cause chain. Missing source/workflow IDs and child
  results without a clip ID fail preparation. Failed preparation is explicit in
  the result, so a successfully completed orchestration cannot conceal acquisition
  failure. Repeated preparation allows reuse only of unsuccessful child executions;
  ready sources reuse their acquired clip.
- A missing named operator in worker configuration was described as proven
  revocation. The error now explains that removal and worker configuration drift
  are both possibilities, with reconnection/configuration recovery guidance.
  Existing authority, credential and channel checks still apply.
- Katcha AI bypassed the shared hosted authentication recovery UI. It now uses the
  existing Cloudflare Access bootstrap, including signed-out/unmapped-identity
  explanations. Chromium, Firefox and WebKit run hosted sign-in and AI recovery
  regressions in CI. These use mocked API responses, not a live Access login.
- The AI can now read public HTTPS HTML/text through the existing public-IP-pinned
  retrieval service. This runs on the server and does not depend on the operator's
  browser. Receipts include retrieval time, content hash and truncation. Private
  addresses, credentials, unsafe redirects and unsupported content remain rejected.
  This is page reading, not an interactive signed-in browser or video playback.
- Planning freezes independent retrieval targets. Completion requires an observed,
  acquired clip in the selected channel for every target. Duplicate clip IDs do
  not establish different scene matches. Exact scene/action requests remain blocked
  when only candidate acquisition or sampled stills exist. Fresh planner responses
  must declare the target list; old saved decisions retain compatible defaults.

## Evidence boundaries and remaining live work

Read-only inspection of all three production histories on 2026-10-17 established
that YouTube rejected the downloads with “Sign in to confirm you’re not a bot.”
The Kate Bishop preparation attempted videos `PRBwFwZC6kU` and `wkqOBZnrsQY`;
both failed in `ingest_source` with that challenge. The Ultron preparation used
`PRBwFwZC6kU` again and failed the same way. Its media source
`9c4de52a-4d47-4787-97a3-3c8172df60ab` retained only “Activity task failed” and
had no acquired clip. This proves an acquisition authentication challenge, not a
verified scene match or a problem caused by the operator's browser.

The API, intelligence worker and ingestion worker reported the same deployed
release `ae861124cf8747680c5fb70540e547415023095a`, no legacy named control
principals, and the configured Cloudflare principal `operator-ui`. The three
historical revoked-error goals were actually bound to
`control-principal:cloudflare-access:operator-ui`. They predate accepted commit
`b18cdbc6` (Cloudflare goal-authority binding, 2026-10-15), supporting the historical
authority-routing defect rather than a finding of deliberate operator revocation.
Current Cloudflare authority handling has dedicated regression coverage. The
original 2026-10-14 generic planning failure has no specific provider cause saved
in its goal receipt; current planner tests cover categorized provider errors.

The existing tunnel initially closed. After explicit operator authorization, the
guarded access command created one temporary session on the existing OCI Bastion.
Production queries were read-only. No credential contents, browser cookies,
provider calls, downloads, production configuration changes or service restart
were used for this investigation.

Recognized YouTube sign-in challenges now carry a stable
`youtube_authentication_required` code, a server-session/upload recovery explanation,
and non-retryable Temporal application failures. A separate explicit retry remains
possible after authentication changes. Other download errors retain their normal
retry behavior.

An optional `KATCHA_YOUTUBE_DOWNLOAD_COOKIE_FILE` accepts an operator-managed
Netscape cookie jar on the server. It never extracts a session from the browser
running Katcha. Mount the file read-only into the ingestion worker outside the
checkout, set that environment variable to its container path, and keep the file
out of Git and chat. For example, an operator-reviewed Compose override may add:

```yaml
services:
  worker:
    environment:
      KATCHA_YOUTUBE_DOWNLOAD_COOKIE_FILE: /run/secrets/youtube-download.cookies
    volumes:
      - type: bind
        source: /etc/katcha/youtube-download.cookies
        target: /run/secrets/youtube-download.cookies
        read_only: true
        bind:
          create_host_path: false
```

That is a configuration example, not a production change performed by this task.
Use a session the operator is authorized to use. The downloader copies it into a
private per-job directory/file (0700/0600), allows yt-dlp to update only that
copy, excludes it from media detection, and removes it on success or failure.
A configured cookie jar does not guarantee acceptance of a particular video,
server IP, region or session. An authorized direct file upload is another
existing intake path. Provisioning/replacing a session and a supervised rollout
still need their own operator action; no such credentials were provided here.

Automatic verification of characters performing an action through time is still
unavailable. Sampled-still semantic hypotheses and web metadata do not supply that
proof. A future temporal-video inspection capability needs explicit coverage,
evidence coordinates, uncertainty and authorized external inspection. This change
prevents false completion; it does not claim to implement that capability or clear
media rights. A merged PR is not evidence of production deployment.
