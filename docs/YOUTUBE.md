# YouTube integration

Katcha treats YouTube as a credentialed publishing integration, not as an upload script. OAuth credentials are encrypted at rest, resumable upload state is persisted, publication is private-first, and analytics are tied back to the exact production generation that created the video.

## Google setup

1. Create/select a Google Cloud project.
2. Enable the YouTube Data API v3 and YouTube Analytics API.
3. Configure the OAuth consent screen.
4. Create an OAuth 2.0 Web application client.
5. Add Katcha's configured callback URI (development default: `http://localhost:8000/v1/integrations/youtube/oauth/callback`).
6. Put the client ID/secret in your private `.env`; never commit them.

Generate the database credential-encryption key once and store it with your deployment secrets:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the resulting value as `KATCHA_CREDENTIAL_ENCRYPTION_KEY`. Losing this key makes stored OAuth and upload-session credentials unreadable, so back it up in the same secret-management system as the OAuth client secret.

## Scopes

The default connection requests YouTube management access plus read-only YouTube Analytics access. Monetary analytics is deliberately opt-in with:

```text
KATCHA_YOUTUBE_INCLUDE_MONETARY_SCOPE=true
```

That keeps the default OAuth request smaller while still allowing YPP/revenue collection when a channel and Google project are ready for the additional scope.

## Connect a channel

Start the flow with:

```text
GET /v1/integrations/youtube/oauth/start
```

Open the returned `authorization_url`. Google redirects back to Katcha, which exchanges the authorization code, resolves the YouTube channel identity, encrypts the access/refresh tokens, and upserts the connection by YouTube channel ID.

Normal API responses never expose access tokens, refresh tokens, PKCE verifiers, or resumable-upload URLs. Temporal workflow histories receive UUIDs and timing metadata only; provider credentials are resolved inside activities.

## Publish an approved production

Publishing requires an approved production with a `render` asset. Create a publication with:

```text
POST /v1/productions/{production_id}/publications
```

The initial upload is always private. `privacy_status` describes the desired post-processing state:

- `private`: remain private after processing.
- `unlisted`: switch to unlisted after processing.
- `public`: publish after processing, or schedule with `publish_at`.

When `publish_at` is supplied it must be a future timezone-aware timestamp and the final privacy status must be `public`.

The upload workflow creates a resumable session once, persists it encrypted, and records YouTube's acknowledged byte offset after each chunk. Transfer retries query the provider before sending more bytes. An expired partially-used session fails visibly rather than silently starting a second upload.

Normal retries use:

```text
POST /v1/publications/{publication_id}/retry
{}
```

If YouTube already assigned a video ID, Katcha skips upload and resumes status/finalization work. If the resumable session itself expired before Katcha learned a video ID, the state is ambiguous: YouTube may have accepted the upload even though Katcha cannot prove it. Katcha therefore refuses to create a new upload session until an operator explicitly acknowledges the duplicate-video risk:

```text
POST /v1/publications/{publication_id}/retry
{"allow_new_upload_session": true}
```

That acknowledgement is recorded in the publication event history, and the retry receives a new workflow-attempt ID.

## Analytics

After finalization, Katcha starts the configured observation schedule (development default: 1h, 6h, 24h, 72h, 7d, 30d). Each observation has a deterministic `sample_key`, so Temporal/activity retries converge on one database snapshot.

Normalized snapshots include views, engaged views where supported, watch time, average view duration/percentage, likes, comments, shares, subscriber gains/losses, and optional monetary metrics. The raw provider payloads are retained for forward compatibility. Retention points are stored separately by `elapsedVideoTimeRatio`.

Manual refresh:

```text
POST /v1/publications/{publication_id}/analytics/refresh
```

Read snapshots and retention curves:

```text
GET /v1/publications/{publication_id}/analytics
```

Analytics failures are recorded as publication events but do not retroactively turn a successfully published video into a failed publication.
