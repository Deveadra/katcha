# Acquisition verification notifications

The existing Telegram worker now scans persisted acquisition challenges in addition
to editorial review deliveries. A download waiting for human verification sends a
single **Open verification** link to the paired operator. The link opens Katcha's
authenticated recovery page; it contains only the channel and job identifiers.
Telegram does not carry browser cookies, passwords, source URLs or a button that
asserts the provider has verified access.

## Configure delivery

Use the existing Telegram bot configuration and operator pairing. Add the following
to the Telegram worker's environment, substituting the actual application origin
and explicitly admitted channel UUIDs:

```dotenv
KATCHA_TELEGRAM_ENABLED=true
KATCHA_TELEGRAM_ACQUISITION_APP_ORIGIN=https://katcha.example
KATCHA_TELEGRAM_ACQUISITION_CHANNEL_IDS=["00000000-0000-0000-0000-000000000001"]
```

The application origin must be HTTPS, without a path, query, fragment, credentials
or nonstandard port. The existing `KATCHA_TELEGRAM_BOT_TOKEN` and either operator
pairing or the existing chat/user settings are also required. Keep credentials in
the existing secret delivery mechanism. A paired chat alone does not admit every
channel: an empty channel list sends no acquisition notifications.

Deploy the acquisition challenge migration before starting this version of the
Telegram worker. Recovery also requires the API and acquisition worker recovery
configuration; notification configuration alone cannot enable browser access.
The worker scans while it runs, alongside its normal bounded Telegram polling.
Restart it after changing environment configuration.

## Delivery states

- `pending`: delivery has not been attempted.
- `not_configured`: bot, origin, pairing or channel admission is missing. The worker
  checks again on later passes; the recovery page remains usable independently.
- `sending`: a durable send claim prevents another worker from sending a duplicate.
- `sent`: Telegram returned a message identifier.
- `unknown`: the request failed ambiguously, or a process died during a send. The
  worker does not automatically resend because Telegram supplies no idempotency key.
- `expired`: the verification window ended before notification delivery.

The same challenge is never resent on each poll. Each new bounded verification
round may create a new challenge and notification. Cancelled, replaced, expired
or channel-mismatched challenges are excluded. The link never substitutes for
Katcha authorization: opening it still requires a permitted operator session.

Offline tests cover delivery deduplication, uncertain responses, crash recovery,
channel admission, expired challenges and scanner fairness with more than one
page of unconfigured channels. They do not send live Telegram messages or prove
production delivery; verify that separately with an authorized test job after
configuration and deployment.
