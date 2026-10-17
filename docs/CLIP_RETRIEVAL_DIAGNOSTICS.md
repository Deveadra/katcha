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

The exact downloader failure in the supplied production histories is **not yet
established**. The existing verified SSH tunnel initially listed production
containers, then refused connections on localhost port 22022 before those histories
could be read. No new Bastion session, credentials, provider calls, downloads,
production configuration changes or restart were performed for this investigation.
Code inspection establishes the diagnostic defects above; it does not establish
whether the incident's download failed because of authentication, region, video
availability, network, media inspection or storage.

After an operator restores the approved production access path, inspect all three
histories and their nested failures, source diagnostic events, and exact deployed
release/worker revisions. Do not silently retry unknown acquisition outcomes.
If desired, rerun acquisition only with authorized source access and budget.
A configuration snapshot or successful page read is not a successful media download.

Automatic verification of characters performing an action through time is still
unavailable. Sampled-still semantic hypotheses and web metadata do not supply that
proof. A future temporal-video inspection capability needs explicit coverage,
evidence coordinates, uncertainty and authorized external inspection. This change
prevents false completion; it does not claim to implement that capability or clear
media rights. A merged PR is not evidence of production deployment.
