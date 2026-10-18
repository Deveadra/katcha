# Operator verification and Telegram-assisted recovery — implementation plan

**Status:** Design only. No host, credentials, production workflow, or bot behavior changed.

**Goal:** Keep Katcha working 24/7 while a single eligible job awaits human
verification. Notify the operator in Telegram, link to the provider's real
verification flow, verify the outcome independently and wake the original
Temporal workflow. A human challenge solution does not permit access that
YouTube or another platform otherwise prohibits.

## 1. Existing implementation inspected

- src/katcha/integrations/telegram.py: send_message, get_updates (messages and
  callback_query), answer_callback, edit_text, edit_caption, clear_buttons.
  Extend the existing authorized bot; no competing getUpdates consumer.
- src/katcha/integrations/download.py: yt-dlp, typed authentication failures,
  optional proxy and cookie-file fallback on YouTube sign-in challenges.
  Those fallbacks do not constitute proof of permission to scrape/download.
- src/katcha/ops/download_route.py: optional managed SSH SOCKS route.
- src/katcha/orchestration/activities.py: ingestion diagnostic, persistence,
  failure handling and media storage.
- src/katcha/orchestration/workflows.py: ClipIngestWorkflow currently makes
  up to four activity attempts, with no durable human-wait state.
- src/katcha/orchestration/worker.py: starts existing ingest/publish workers
  and reconciles interrupted jobs at startup.
- .env.example: existing Telegram credentials, chat/user binding and
  Cloudflare Access operator identity configuration.
- PR #486: pending separate-egress preflight design. Issue #488: host work.

The major missing capability is not sending a Telegram message; it is
proof-backed, persistent, idempotent resolution of a paused operation.

## 2. Policy-aware challenge taxonomy

Classification must precede any pause, prompt or retry.

| Failure class | Handling |
|---|---|
| Supported OAuth/consent/MFA reauthorization | WAITING_HUMAN, notify, verify official callback |
| Source/operator rights approval | WAITING_HUMAN, require rights provenance |
| Permitted provider challenge with verifiable postcondition | WAITING_HUMAN, link to provider-owned page |
| Rate limit / Retry-After | Defer without CAPTCHA prompts; honor limits |
| Transient DNS/network/worker failure | Bounded automatic retry and alert on exhaustion |
| YouTube bot-check on restricted automated download | BLOCKED_PLATFORM_POLICY; retain reference |
| Content unavailable/forbidden/DRM/geo-restricted | BLOCKED_POLICY or manual licensed-source path |

YouTube's API rules prohibit downloading/storing YouTube audiovisual content
without its prior written approval, as well as scraping. A creator's license
does not automatically waive the platform's own restrictions. Katcha can
alert the operator and offer an original file, licensed feed, approved owner
export or research-only URL instead of trying to transfer a solved challenge
to its downloader.

Never relay CAPTCHA answers, tokens or browser cookies through Telegram,
rotate IPs to bypass blocking, or automatically retry a disallowed downloader
merely because the operator pressed a Telegram button.

## 3. End-to-end operator flow

1. Activity encounters a typed recoverable challenge and records
   provider, purpose, workflow ID, source ID, channel, correlation ID,
   attempt ID, policy decision and expiry; record is committed first.
2. Transition only this job to WAITING_HUMAN and pause further retries;
   other channels, scouting, publishing and analytics continue.
3. Outbox sends one deduplicated Telegram message: provider, operation,
   concise problem, job reference and absolute expiry.
   Buttons: Open secure verification, I completed verification,
   Provide licensed source, Skip or Cancel.
4. Open secure verification leads to a short-lived authenticated Katcha
   operator URL. Human completes provider-supported verification at the
   provider's legitimate domain. Challenge imagery is not copied into
   Telegram because many challenges are tied to original browser/session/IP.
5. Provider OAuth callback or explicit recheck validates authorization.
   I completed verification asks Katcha to CHECK; it is not a success claim.
6. After a verified provider result and independent rights/policy gate,
   transactionally persist VERIFIED_AUTHORIZED and signal the same Temporal
   workflow with verification-request ID and state-version.
7. Temporal resumes the original job from the last safe checkpoint; it
   rechecks current authorization, source rights and lease; no duplicated
   downloads, spending, publications or asset records.
8. Telegram original message and Katcha activity card become VERIFIED,
   RESUMING and COMPLETED with independent timestamps and errors.
9. On rejection, unknown proof or still-forbidden operation, remain
   WAITING_HUMAN or finish BLOCKED_PLATFORM_POLICY with a safe alternative.
10. If the operator is away, keep the specific job parked until expiry.
    Do not suspend autonomous operation globally or keep a live HTTP
    transfer open during the wait.

## 4. Durable model and workflow semantics

Additive Postgres tables (final columns require migration-base review):
- operator_verification_requests: id, provider, purpose, challenge_kind,
  source_id, channel_profile_id, workflow_id, workflow_run_id, attempt_id,
  state, version, policy_decision, principal, created_at, expires_at,
  verified_at, last_error_code, resolution_evidence_ref, nonce_hash,
  telegram_message_id/chat_id, correlation_id.
- operator_verification_audit: request ID, actor, transition, reason,
  event time, provider-status code and trace ID.
- notification_outbox: request ID, state version, delivery status, attempt
  count and unique idempotency key.

Suggested states:

    RUNNING -> WAITING_HUMAN -> NOTIFIED -> VERIFYING
                   |                         |   |
                   |                         |   +--> NOT_CONFIRMED
                   |                         |            -> WAITING_HUMAN
                   |                         v
                   |                    VERIFIED_AUTHORIZED
                   |                         |
                   |                    SIGNAL_SENT -> RESUMING -> RUNNING
                   |
                   +--> EXPIRED | CANCELLED | ACTION_REQUIRED_MANUAL_SOURCE
                   +--> BLOCKED_PLATFORM_POLICY

- CAS/unique constraints prevent two callbacks resuming one source twice.
- Verified means provider postcondition, not Telegram acknowledgment.
- Signal-sent is not identical to resumed; the activity completion is
  independently observed and audited.
- Temporal version/patch compatibility required for historic workflows.
- Never create a second workflow on boot for an existing WAITING_HUMAN job.
- Timeout, cancellation, token refresh, revoke and restart are explicit.

## 5. Security and human interaction

- Reuse the configured Telegram allowed chat ID and allowed user ID.
  Telegram callbacks are scoped to operator, request, purpose, version
  and TTL; single-use confirmation actions.
- Katcha's secure verification page remains behind Cloudflare Access and
  maps to a scoped Katcha operator principal. Use CSRF and one-time
  nonce/OAuth state checks, minimal permissions and HTTPS.
- Never include cookies, CAPTCHA tokens, OAuth refresh tokens, R2 signed
  URLs, passwords, MFA codes or session material in messages, events,
  generic HTTP errors, issue attachments or logs.
- The provider challenge is interacted with only through an authentic
  browser/provider page. If no permitted, verifiable human handoff
  exists, show a manual-source alternative rather than false success.
- Respect Retry-After, platform robots/access restrictions, identity and
  regional controls; do not spoof client identity or rotate IPs.
- Add spam control: one live alert per request, sensible reminders,
  expiry notifications, and a persistent web inbox if Telegram is down.
- User may manually supply a rights-cleared file or provider-authorized
  source. Rights and malware/size/codec checks remain mandatory.

## 6. Code change batches

**P1 — Classification and policy:** src/katcha/integrations/download.py,
src/katcha/orchestration/failure_details.py, source policy checks. Distinguish
allowed account reauth from restricted YouTube automated acquisition.

**P2 — Database and events:** New additive Alembic migration and verification
request/outbox service. Persist challenge-before-notify, dedupe, explicit
deadline and redacted domain event.

**P3 — Temporal:** Introduce WAITING_HUMAN, request-specific signal, safe
workflow checkpoint/replay, state reconciliation and testable cancellation.
Do not modify existing workflow histories without versioning.

**P4 — Telegram and UI:** Extend the existing bot update consumer and
TelegramBotClient, not a second polling consumer. Add secure URL buttons,
request-check callback and message updates. Add queue/activity cards,
operator verification inbox, skip/manual source/cancel actions.

**P5 — Provider verification:** Official OAuth reauthorization callbacks
and proof-backed provider/API checks, tied to an exact operator request.
No generic endpoint that simply marks solved.

**P6 — Acquisition host:** In parallel with issue #488, scope the remote
service principal, secure task-lease/heartbeat mechanism, isolated network,
no exposed DB/Temporal/admin ports, route/R2 staging and 3+ GiB resumable
transfer with SHA-256/ffprobe validation. Activation requires live cost
inventory, approved network plan and production change gates.

**P7 — Observability/release:** Structured status/events, message-delivery
latency, proof success/failure, workflow signal-to-resume latency,
waiting queue age, R2 state, repeated blocks, operator decision history,
feature flag/kill switch, canary and rollback.

## 7. Acceptance tests and completion criteria

- Supported mocked account reauthorization: WAITING_HUMAN -> Telegram ->
  official callback -> recheck -> signal -> original workflow resumed.
- Telegram button without provider proof: remains waiting, not completed.
- Wrong Telegram user/chat, wrong Cloudflare operator, expired callback,
  reused state or mismatched job: forbidden and audited.
- Worker restart, lost response, duplicate Telegram callback and duplicate
  provider callback: exactly one successful resume and no duplicate media.
- Disallowed YouTube download: permanent policy block, no cookie/proxy
  bypass; licensed-original option and research reference retained.
- Telegram API unavailable: job remains durable, error diagnostic shown;
  other channels keep operating; notification sends when transport returns.
- If provider verification succeeds but rights fail: stays blocked.
- If a job is cancelled or expires before provider callback: never resumes.
- Authorized 3+ GiB media path: resumable across reboot; hash, ffprobe,
  dedupe and rights/provenance recorded; no unlimited memory/disk use.
- No secrets in logs, Telegram, workflow histories, or domain events.
- Healthy-system SLO targets: notification under 30 seconds after commit;
  verification-confirmed to Temporal signal under 10 seconds; signal to
  resumed activity under 10 seconds. These are targets, not guarantees.
- Existing production poller stays disabled; reconcile known production
  database migration gate independently before rollout.
- Synthetic -> approved account -> authorized media -> one channel canary
  -> monitored 24/7 rollout. No activation before security/CI review.

**Done means** a real permitted account reauthorization through Telegram
and legitimate browser is provably followed by an idempotent resume of the
original Temporal job; independent egress and approved-media ingestion are
separately validated in the target host. A solved CAPTCHA is never treated
as blanket permission to automate a YouTube download.

Related: issue #488; PR #486; docs/YOUTUBE.md; docs/INGESTION.md.
References: https://core.telegram.org/bots/api ,
https://developers.google.com/youtube/terms/developer-policies ,
https://www.youtube.com/t/terms .
