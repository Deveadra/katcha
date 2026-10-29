# Human verification for acquisition

This is an implemented recovery path for the existing scoped remote acquisition
worker. It uses the original acquisition job, source, channel and receipt. It does
not attach a personal phone browser or Katcha AI's separate server browser to the
downloader. The verification browser runs on the acquisition machine and exports
only its own approved-domain cookies locally for one explicit download retry.

## Operator journey

1. An approved acquisition encounters an authentication challenge. If the worker
   has a working recovery browser and the API has recovery enabled, the job enters
   `waiting_human`. Its receipt stays `acquiring`; no clip is claimed as acquired.
2. The existing Telegram worker sends the admitted operator an **Open verification**
   link. Opening it requires Katcha authentication and access to the original channel.
   The link contains only channel/job identifiers. AI goal delegation cannot use
   the recovery controls or read browser results.
3. Choose **Open verification**, complete the site's prompts in the displayed
   browser, and choose **Check access and continue**. Commands use the worker's
   existing outbound HTTPS connection; no inbound workstation port is opened.
4. Katcha rechecks the download permission and original worker identity, fences the
   old generation, and permits exactly one new acquisition attempt. The host uses
   that browser's cookies and User-Agent and removes the temporary cookie files.
5. The existing multipart upload, independent hash/media validation and channel
   handoff decide success. The existing Temporal acquisition workflow continues
   polling the same durable job and releases its waiting parent only after validated
   completion. No notification click or browser snapshot can assert success.

A lost form-action response never automatically replays the form. Refresh the
result or obtain a fresh snapshot before acting again. A lost resume response
returns the same generation; interrupted extraction is quarantined. Browser
restart loses sign-in state and requires reopening verification. Cancellation
fences commands/results and clears the encrypted mailbox. A verification round
expires after 30 minutes; a job admits at most three rounds. It then ends with a
visible failure rather than retrying indefinitely.

## Wire the deployment

Deploy one tested release across the API, primary ingestion worker, Telegram
worker and workstation worker. Run migrations through
`0067_acquisition_human_recovery` before starting the new processes. The migration
adds recovery tables and expands a status constraint; it does not discard jobs or
media. Downgrade refuses to discard populated recovery history.

On the API and primary ingestion worker:

```dotenv
KATCHA_REMOTE_ACQUISITION_ENABLED=true
KATCHA_ACQUISITION_HUMAN_RECOVERY_ENABLED=true
```

Retain the existing `KATCHA_CREDENTIAL_ENCRYPTION_KEY` through the existing secret
mechanism. It encrypts the short-lived browser mailbox at rest; never copy that
key to the workstation. Completed/expired commands are not stored in AI receipts
or audit payloads. The primary acquisition supervisor erases expired mailbox
payloads on its regular pass while recovery is enabled.

Set `KATCHA_REMOTE_ACQUISITION_AUTO_DISPATCH_ENABLED=true` on the API and primary
worker only after all intended channels have an admitted worker and approved
acquisition receipts. With automatic dispatch disabled, explicitly queued jobs
still use this recovery path, but ordinary legacy ingestion does not silently
switch hosts. The original historical failed job is not automatically replayed.
An operator must retry it through an approved source receipt after rollout.

On the workstation, enable `KATCHA_ACQUISITION_HUMAN_RECOVERY_ENABLED=true` and
install the `browser` extra plus the matching Chromium executable and OS libraries.
See [workstation setup](ACQUISITION_WORKSTATION.md) for the persistent systemd unit,
private state directory and narrow control/Cloudflare credentials. A headless
browser may still be refused by a source; this feature does not promise that a
particular CAPTCHA, hardware key or provider restriction can be completed remotely.

Configure the existing Telegram service using
[acquisition notifications](ACQUISITION_NOTIFICATIONS.md). Admit channel UUIDs
explicitly and set the HTTPS Katcha app origin. Missing notification configuration
is shown on the recovery page; the page works independently of Telegram delivery.
An ambiguous Telegram send is marked unknown rather than automatically duplicated.

## Acceptance and rollback

Before enabling automatic dispatch, prove this sequence on an authorized source:
challenge → waiting job → one notification → authenticated browser interaction →
one retry → uploaded bytes → independently validated clip in the original channel.
Also prove cancellation, workstation restart, expired sessions and Cloudflare
control reconnection. Confirm the machine stays available under its systemd
supervision. No paid resource, new provider or production activation is created by
these source changes.

The offline suite exercises real API authorization, persistence, the worker,
notification outbox, upload protocol and validation with synthetic provider/storage
transports. Browser fixtures exercise the recovery page, mobile layout, keyboard,
connection errors and uncertain actions. These tests do not prove live YouTube
acceptance, media rights, Telegram delivery, production deployment or scene identity.

To drain, disable new automatic dispatch first. Keep the existing API, supervisor
and recovery controls available until waiting jobs are completed or cancelled;
then disable recovery and stop the workstation service. Preserve the durable job
journal and migration history. Never downgrade populated recovery tables or replay
an unknown source execution to clear a warning.
