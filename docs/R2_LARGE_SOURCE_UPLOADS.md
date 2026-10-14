# Large source-video uploads (R2 multipart)

Katcha's existing source uploader remains the default for media up to **512 MiB**.
Editorial Studio now uses S3-compatible, **presigned multipart PUT** for larger
MP4/MOV/M4V/MKV/WebM media up to **4 GiB**, including 3 GiB 4K trailers.
Parts are 64 MiB with at most two browser transfers at once. The user can
reselect the same file and title to recover uploaded parts after a network,
tab or API response interruption. The original source Clip Library binding,
permission confirmation, deduplication and project creation flow remain.

Large-file bytes go **browser -> R2**, not browser -> Katcha -> R2 or the
Cloudflare Worker. Only upload control/sign/finish/status calls hit the API.
An authenticated, channel-scoped SourceItem acts as the durable journal.
Katcha completes multipart only after it checks R2's part inventory for
exact count, order and sizes, then streams the staged object to compute
SHA-256 and uses ffprobe over a short-lived private S3 GET before promoting
it into the same canonical content-addressed `raw/<sha-prefix>/<sha>.ext`
namespace. The stream uses 8 MiB chunks rather than placing a multi-GiB
copy on OCI's system disk. Promotion preserves existing Clip Library lineage.
An uploaded file is **not automatically production-cleared**.

## One-time production setup

1. Verify the OCI production API is configured for the existing
   `katcha-media-prod` R2 bucket with working, non-browser S3 credentials.
   Do not expose any R2 key or token in HTML/JavaScript or logs.
2. Add this server-side deployment environment variable pointing to the
   **R2 S3 API origin**, not the public bucket/custom content domain:

   `KATCHA_S3_BROWSER_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

   The host must be browser-resolvable; **HTTPS is mandatory in production**.
   Use the same account endpoint as the server's S3 client. Leave it unset to
   disable large uploads explicitly (small uploads are unaffected).
3. Cloudflare Dashboard -> R2 Object Storage -> `katcha-media-prod` ->
   Settings -> CORS policy. **Merge** the following rule with existing rules
   rather than overwriting unrelated policies. Replace the example origin
   with the *exact origin in your browser*, including port when present:

   ```json
   [
     {
       "AllowedOrigins": ["https://YOUR-KATCHA-APP-ORIGIN"],
       "AllowedMethods": ["PUT"],
       "AllowedHeaders": ["Content-Type"],
       "ExposeHeaders": ["ETag"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

   For WSL/local testing, add the *specific* local launcher origin, not `*`.
   See [Cloudflare R2 CORS](https://developers.cloudflare.com/r2/buckets/cors/).
   Do not alter production R2 CORS until the operator has reviewed the
   existing policy and confirmed the correct browser origin.
4. R2 aborts incomplete multipart uploads after 7 days by default; verify
   the existing policy is still enabled. Add a **separate**, prefix-scoped
   expiry for orphaned *completed* `upload-staging/` objects after an
   appropriate recovery window; preserve existing backup/raw lifecycle
   rules. Keep enough time for slow transfers and an operator's retry.
5. Deploy the tested PR through the ordinary Katcha Guardian CI/merge flow.
   None of these steps is performed by merging source code automatically.

## Scoped control API

All endpoints share `/v1/channels/{channel_id}/editorial-projects/source-uploads/multipart`.

- `POST /` creates/replays a journal with
  `{filename,title,size_bytes,content_type,idempotency_key,permitted_use:true}`
- `GET /{source_id}` retrieves status and the R2-confirmed set of received
  part numbers, ETags and sizes, without signing any new PUT.
- `POST /{source_id}/parts` with `{part_number}` signs one exact part for
  ten minutes, after the current deployment-leader fencing check.
- `POST /{source_id}/complete` completes and verifies *all* parts from
  R2's inventory and registers the clip, returning the source binding.
- `DELETE /{source_id}` aborts an unfinished upload, or discards a failed
  verification after its worker has stopped, and removes staging data.
  A competing live verifier receives a conflict instead of being interrupted;
  completed canonical media cannot be deleted here.

The API requires the authenticated channel plus `production:create` for
mutations; read/status requires `ai:read`. Presigned part URLs are bearer
capabilities valid for at most ten minutes. An already issued signature
cannot be immediately revoked by Katcha leadership fencing, but completion
and registration are fenced. New URLs are never minted by stale leaders.

## Validation and operational limits

- A 3 GiB example requires 48 x 64 MiB PUTs. All but the last part must
  have the full part size, and no video may exceed 4 GiB.
- Failed parts receive bounded retries; reselect the *same* file to resume.
  Upload identity includes file size/name/mtime/title, channel scope, and
  bounded SHA-256 fingerprints of the first and last 1 MiB. The browser never
  loads a multi-GiB file into RAM to calculate a digest; the server hashes the
  entire received file before Clip Library registration. Re-select the same
  file and title to resume, or use **Discard failed large upload** after an
  error to clean staging and restart.
- Multi-GiB SHA-256 verification may take significant wall time: the UI
  distinguishes the completion and verification phases. PostgreSQL advisory
  locking prevents duplicate verifier workers for the same source and releases
  automatically if a worker stops. Do not force-kill the OCI API during
  verification; retries recover a completed staged object without reuploading.
- S3/Cloudflare control-plane credentials must remain server-only.
  Browser devtools will contain **temporary signed part URLs**, not permanent
  credentials. Do not include presigned URLs in bug reports.
- Run `pytest -q tests/test_editorial_multipart_uploads.py` and
  `node --check src/katcha/web/editorial.js` along with normal PR Gate.
  Fixture storage checks are **not proof of a live 3 GiB R2 transfer**.
  Production acceptance requires an owned/permitted real 3 GiB file,
  interrupted/retried browser transfer, canonical SHA-256 and ffprobe
  confirmation, project binding, and no production-primary resource pressure.
