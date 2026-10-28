# Read-only verification of an uploaded trailer in production R2

**Purpose:** establish whether an operator-uploaded video really exists in the
canonical Clip Library, its complete bytes are readable from the configured
production R2 bucket, and the actual video matches the recorded metadata.
This is the evidence gate before using a trailer for KQA-101 real-media tests.

The implementation is \`python -m katcha.ops.verify_content_upload\`. It does
**not** upload, retry, copy, promote, delete, index, render or publish anything.
It performs scoped PostgreSQL SELECTs, one R2 object HEAD and one bounded object
GET, SHA-256 verification, and ffprobe over a private temporary file removed
afterward. No bucket-wide listing, paid AI requests or YouTube calls are made.

## Find the exact ID and choose the correct path

1. In Katcha, open **Content** for the intended channel. Under the trailer
   record's **Troubleshooting**, copy the **Content ID**. The channel ID is in
   the URL's \`channel\` query field. Choose \`--content-id\` **only** if the
   trailer was submitted through Content's Add content file workflow.
2. If instead the footage was uploaded through **Editorial → Assets** (the
   small source uploader or direct-to-R2 multipart uploader), use the completed
   **Source ID** returned by that uploader or its status endpoint. Choose
   \`--source-id\`. Do not infer it from a filename or provider URL.
3. Both routes must point to an original channel-scoped, **completed** operator
   upload and a canonical Clip. Uploaded bytes alone, a staging R2 object, a
   discovered URL, or an incomplete progress bar are not acceptance.

Use a shell **inside the already configured Katcha application environment**,
where the process has its existing PostgreSQL and production R2 credentials.
Do not copy service tokens into a chat, GitHub issue, terminal transcript or
command-line flag. Verify the installed application source includes this
command; a successful repository CI run does not prove the running release was
deployed. No deployment is performed by this procedure.

For a **Content** upload:

\`\`\`bash
python -m katcha.ops.verify_content_upload \
  --channel-id CHANNEL_UUID \
  --content-id CONTENT_UUID
\`\`\`

For an **Editorial** upload:

\`\`\`bash
python -m katcha.ops.verify_content_upload \
  --channel-id CHANNEL_UUID \
  --source-id SOURCE_UUID
\`\`\`

When the original permitted file is retained locally, calculate its independent
SHA-256 (e.g. \`sha256sum /path/to/authorized-trailer.mp4\`) and pass the digest
as \`--expected-sha256 64_HEX_CHARACTERS\`. This comparison is stronger than
only checking the database's expected digest against R2.

The tool **requires** the configured storage to be the HTTPS Cloudflare R2 S3
endpoint and \`katcha-media-prod\`. It rejects local MinIO, backup buckets,
missing or nonready records, changed permission evidence, unrelated channels,
noncanonical source references, source/Clip inconsistencies, missing or changed
objects, corrupt bytes and incorrect video dimensions/duration. It reads a
maximum of **200 MiB**, matching the measured SceneIndex's current limit.
Larger media may already be in R2 but require a separately designed bounded
inspection path; do not raise this limit casually on the OCI primary.

## Interpreting proof

Exit code **0**, and JSON \`status: "verified"\`, establish the exact stored
bytes, SHA-256, size, and actual ffprobe dimensions and duration for the
specific Content/Source and Clip IDs at verification time. The JSON includes:

- \`upload_kind\`: \`content\` or \`editorial\`
- \`channel_id\`, \`content_id\` (null for Editorial), \`source_id\`, \`clip_id\`
- \`sha256\`, \`size_bytes\`, \`video\`, \`verified_at\`
- \`original_sha256_compared\` (false unless independently supplied)
- \`editorial_index_size_eligible: true\` (size only; **not** permission)
- \`rights_independently_verified: false\` and \`production_cleared: false\`
- \`real_editorial_index_executed: false\` and \`real_remotion_render_verified: false\`

It never includes R2 keys, tokens, signed URLs, database connection strings
or the operator identity. A "verified" report confirms byte integrity, **not**
legal redistribution rights, semantic accuracy, a valid source-to-project
binding, a real SceneIndex result or a completed video.

Exit code **2** and \`status: "blocked"\` indicate a bounded, named validation
failure; exit code **3** reports a generic unavailable internal check without
leaking secrets. Preserve the safe JSON reason and inspect the original record.
Do not delete, reset or blindly reupload a stalled/ambiguous receipt. A
\`not_configured_for_production_r2\` failure requires checking the process
configuration, not bypassing production safety checks.

## Next required acceptance evidence

After a **verified** report, with actual inspection permission established:

1. Add the **existing canonical Clip ID** to the appropriate FORESCENE
   Editorial project's Media Bin. This is a reference, not an additional copy.
2. Run **Inspect acquired footage → Index footage** and record the saved
   \`SceneIndexV1\` ID/digest, actual decoded frames, measured source timebase,
   detected cut intervals and explicit sparse coverage. A <200-MiB clip may
   still fail decoding or permission gates.
3. Produce a human-reviewed timestamp annotation manifest *for that SHA*,
   keeping the original copyrighted video private. Measure shot localization,
   recognition precision and false positives; do not label color-only scene
   tags as character/action recognition.
4. Execute the required full script→footage→Storyboard→actual Remotion MP4
   acceptance in a nonpublishing environment. Retain output hash, frame/audio
   integrity checks, rights assertions, gaps and failure receipts. A passing
   ranked-render fixture or this read-only check **does not** fulfill KQA-101.
5. Separately authorize any live external-provider calls, real-footage
   redistribution, production deployment or publication if needed.

Automation and acceptance work must still satisfy
[Issue #452](https://github.com/Deveadra/katcha/issues/452) with actual
per-channel outcomes. No live production acceptance is claimed by this tool.
