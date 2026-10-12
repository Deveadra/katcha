# Measured project media index (KMI-107)

KIN-101 prepares requirements and durable reference links. KMI-107 adds a local,
measured `SceneIndexV1` for existing canonical video Clips. Indexing creates no
new media copy in managed storage and grants no permission. Reference-only
search candidates cannot be inspected.

## Inspection permission and freshness

A channel-scoped ProjectMediaItem must reference an acquired video. Denied,
restricted, revoked, expired and reference-only rights states block inspection.
Inspection requires either the current channel's existing operator-upload
permission (`permitted_use_confirmed`) or a matching latest production-eligible
rights assessment and latest validated video acquisition. The latter route is
conservative: it does not infer review permission from a provider URL.

The service checks permission and canonical identity before storage access,
after reading bytes, after decoding and when reading/searching an index. Actual
streamed bytes must match the Clip's size and SHA. Changed canonical hashes or
policy versions select a new index identity. Corrupted object bytes block saved
results even when the database hash has not changed. Old evidence is retained
for audit; it does not pass the current-source lookup.

Owned uploads retain `not_assessed` production rights. This index does not
promote any Bin row, alter source selection or feed the renderer automatically.
Export retains its separate validated-acquisition/production-clearance gates.

## What is measured

A PyAV worker decodes every frame of the first video stream, using its exact
source presentation timestamps and timebase. A nonzero source origin is
recorded and normalized to source-relative index intervals. Missing durations,
missing timestamps and non-increasing timestamps fail inspection. Audio, other
video streams, OCR and speech are unobserved.

Each frame is reduced to 64×36 RGB pixels. Mean absolute pixel change of at least
0.22 proposes a cut. Each proposed shot stores its measured half-open interval,
first observed frame timestamp, RGB mean, visual tags and frame-pixel SHA. The
recorded keyframe is the observed frame at the interval start; it is not a claim
about codec keyframes or semantic scenes. Motion, flashes and fades can cause
missed or extra cuts. These intervals are measured visual-change proposals.

Only the start frame of each interval contributes searchable tags. Coverage is
`all_decoded_frames_visual_change_sparse_keyframes`; it does not imply semantic
inspection of every frame or that a tag holds throughout a shot. Exact token
search supports `red`, `green`, `blue`, `dark`, `bright` and `mid-tone`. Unknown
semantic descriptions return no matches. Results include source SHA, index and
shot IDs, measured in/out and keyframe times, coverage and `rights_granted=false`.
No timestamps or unseen actions are produced by an AI model.

## Costs and failure handling

- Source: at most 200 MiB; streams/hash/staging use 1 MiB chunks.
- Decoder: at most 600 measured seconds, 36,000 decoded frames, 512 shots and
  16,777,216 pixels per frame; one decoder thread.
- Linux worker: 60 CPU seconds, 90 wall seconds, 1.5 GiB address space, 32 open
  descriptors and 2 MiB output/log file size per process. One decoder per API
  process is admitted; excess inspection requests fail for an explicit retry.
  Limits apply per API process, not globally across replicas. Timeouts terminate the
  child. The optional `[media]` PyAV dependency is required; missing decoder
  support produces a failed receipt, never fabricated evidence.
- Temporary source/result/log files are removed after success and failure.
  Persistent JSON contains descriptors and receipts, not image/video blobs.
- Search: at most eight selected items, 512 MiB total unique source bytes and
  50 results. Shared Clips are verified once per search.
- External vision, OCR, speech and provider calls: zero.

Index identity includes channel, canonical Clip ID, content SHA and policy.
Unique claims prevent duplicate decoding; completed replay rechecks actual bytes
without another decode. Failed attempts require explicit `retry_failed=true`.
An interrupted `indexing` receipt is not automatically reclaimed. Confirm the
worker has stopped and wait past the decoder window before reconciliation;
then an explicit retry may claim it. Attempt tokens fence a replaced worker
from publishing or overwriting a newer receipt.

## API and consumer contract

Under `/v1/channels/{channel}/editorial-projects/{project}/media-bin`:

- `POST /{item}/scene-index` with `{}` or `{"retry_failed":true}`: inspect/save.
- `GET /{item}/scene-index`: retrieve current evidence after byte verification.
- `POST /{item}/scene-index/reconcile` with
  `{"confirmed_worker_stopped":true}`: record a stopped interrupted attempt.
- `POST /shot-search` with `{"item_ids":["..."],"query":"red","limit":20}`:
  retrieve measured visual-token matches.

Existing channel authorization applies; inspection/reconciliation require write
scope. Limits are server policy, not caller overrides. Missing, stale, unknown
permission or oversized inputs fail closed. Provider URLs and storage keys are
not accepted by these endpoints. KSI-106 can consume `SceneIndexV1` through these
scoped services, retaining evidence IDs and rechecking intended-use rights at
binding/render time.

Deterministic acceptance uses generated three-color footage with cuts at 1 and
2 seconds and a nonzero source origin. Retrieval tolerance is one 10-fps frame
(0.1 seconds). This fixture proves measured retrieval, not real-provider visual
understanding or production clearance.
