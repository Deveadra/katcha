# Standalone Media Library intake

Katcha stores source footage **before** a project exists. Use **Clips / Media Library → Add media**, select a channel, confirm authorized use, and upload a video. No Editorial project, script, edit, render, or publication is created.

- Videos up to **512 MiB** retain the existing Content receipt and 8 MiB resumable chunk path.
- Videos over **512 MiB**, up to **4 GiB**, use the existing authenticated Editorial R2 multipart API directly **without** creating a project. The browser uploads 64 MiB parts with at most two active transfers. A stable request identity includes channel, title, filename, size, modification timestamp, and bounded hashes of the first/last 1 MiB. Reselect the exact same file and title after an interruption; the API discovers already accepted R2 parts.
- After complete, R2 storage is independently SHA-256 verified and ffprobe-validated. A canonical channel-owned SourceItem/Clip is registered. The Content history automatically adopts the SourceItem on refresh. Reuse the Clip from the shared library later without reuploading.
- Production and YouTube publication are **optional separate actions** and still require their existing rights and approval gates. Upload permission does not establish monetization eligibility.
- Failure or Pause does **not** delete uploaded parts or fabricate a ready Clip. Retained receipts are idempotent, channel-scoped, and recoverable. A cancelled/stale request can be reconciled through the existing multipart status/abort API.

## Preflight and acceptance

1. `node --check src/katcha/web/content.js && node --check src/katcha/web/standalone-media-upload.js && node web-tests/standalone-media.cjs`. Existing `web-tests/content.cjs` and browser suite must still pass.
2. Permission denial, R2 write failure/CORS error, no browser R2 endpoint, duplicate selection, pause and tab reload must never claim verified media. The virtual 3 GiB test checks part counts, resumption and idempotency, **not** a live file.
3. On an approved deployment with configured R2 endpoint/CORS, upload owned or properly licensed 3 GiB source footage, interrupt and resume, verify stored Clip digest/bytes/video dimensions, then add the Clip to a new Editorial project. Confirm no project was created at intake and no OCI disk staging occurred. Preserve evidence before declaring live acceptance.

This change does not alter production settings, Cloudflare CORS, R2 lifecycle policy, publication switches, or the production deployment poller.
