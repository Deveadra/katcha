# Katcha Control-Plane Authentication

Katcha supports separate authenticated control principals for the human operator,
Aerith/Ultron, and other automation clients. Principals have independent scopes and
optional channel allowlists.

## Credential model

The original single-token principal shape remains valid:

```json
{
  "name": "aerith",
  "token": "<strong-random-token>",
  "scopes": ["channels:read", "events:read", "events:ack"],
  "channel_profile_ids": ["*"]
}
```

For durable integrations, use labeled credentials instead:

```json
{
  "name": "aerith",
  "credentials": [
    {
      "id": "2026-q3",
      "token": "<old-token>",
      "expires_at": "2026-10-02T00:00:00Z"
    },
    {
      "id": "2026-q4",
      "token": "<new-token>",
      "not_before": "2026-09-29T00:00:00Z",
      "expires_at": "2027-01-01T00:00:00Z"
    }
  ],
  "scopes": ["channels:read", "events:read", "events:ack"],
  "channel_profile_ids": ["*"]
}
```

Credential IDs are audit labels, not secrets. Tokens remain secret values and are
never returned by the control API. Timestamps must be timezone-aware. A credential
is rejected when it is disabled, has not reached `not_before`, or has reached
`expires_at`.

## Zero-downtime rotation

1. Add a new credential under the existing principal while leaving the current
   credential active.
2. Restart/reload Katcha with the updated configuration.
3. Configure Aerith with the new token.
4. Call `GET /v1/control/session` using the new token and verify:
   - the same `principal_name` / stable actor;
   - the expected credential ID;
   - the expected scopes and channel allowlist.
5. Confirm Aerith can resume its existing event consumer key. Event cursors are
   bound to the stable principal actor, not the individual credential.
6. Disable or remove the old credential and reload Katcha.
7. Verify the old bearer token now returns 401.

This overlap avoids an exact simultaneous Katcha/Aerith cutover.

## Audit identity

Named-principal actions keep the stable actor, for example
`control-principal:aerith`. Request-scoped audit events additionally record the
non-secret credential ID and a short SHA-256 fingerprint. This lets operators prove
which rotated credential authorized a command/action without persisting the bearer
token itself.

The authenticated control session returns the active credential ID/fingerprint and
its configured validity window. It never returns raw token material.

## Revocation and failure behavior

- `disabled: true` immediately makes a configured credential ineligible.
- expired and not-yet-active credentials fail with the same generic 401 response as
  an unknown bearer token.
- duplicate credential IDs within a principal are rejected.
- duplicate bearer tokens within or across principals are rejected.
- the existing legacy `KATCHA_CONTROL_API_TOKEN` path remains unchanged when no
  named principal registry is configured.

No database migration is required for credential rotation; the registry remains a
deployment secret/configuration concern.

## Browser sign-in with Cloudflare Access

The operations briefing can send the browser to Cloudflare Access instead of asking
the operator to paste a bearer token. This works when the public app hostname is
protected by a Cloudflare Access self-hosted application. Cloudflare must forward
`Cf-Access-Jwt-Assertion` to Katcha; Katcha verifies its signature, issuer, audience,
and expiry before trusting the email claim.

Configure the Access team origin, application AUD tag, and protected app hostname:

```text
KATCHA_CLOUDFLARE_ACCESS_TEAM_DOMAIN=https://<team>.cloudflareaccess.com
KATCHA_CLOUDFLARE_ACCESS_AUDIENCE=<application-aud-tag>
KATCHA_CLOUDFLARE_ACCESS_APPLICATION_DOMAIN=app.example.com
```

Map each permitted Access email to a named Access principal. That principal's
configured scopes and channel allowlist apply to the browser session. This separate
registry does not change or disable existing bearer credentials:

```json
[
  {
    "name": "operator-ui",
    "email": "operator@example.com",
    "scopes": ["*"],
    "channel_profile_ids": ["*"]
  }
]
```

Store that list in `KATCHA_CLOUDFLARE_ACCESS_PRINCIPALS`.

The Access policy and Katcha's principal mapping are both required. A valid Access
session for an unmapped email receives 403. When Access is not configured, the
briefing explains that sign-in is unavailable and keeps token entry hidden in hosted
production. The local launcher continues to supply its own credential.

## Operational acceptance

After named principals are configured, run the controlled operator/Aerith
acceptance in [CONTROL_PLANE_LIVE_ACCEPTANCE.md](CONTROL_PLANE_LIVE_ACCEPTANCE.md).

The acceptance verifies session negotiation, least-privilege channel access,
principal-bound event consumption, a real Katcha AI command, explicit proposal
confirmation, Temporal workflow completion, audit attribution, and event-cursor
acknowledgement without creating or publishing media.


### Hosted workspace connection

On `app.katcha.stream`, Home, Trends, Clips, Channel Studio, and Production
verify `GET /v1/control/session` using the same-origin Cloudflare Access
browser session and automatically load data only after the server returns an
authenticated actor. A denied session shows a sign-in or operator-mapping
explanation. Explicit bearer entry remains a fallback; the local launcher does
not use this browser bootstrap.

**Deployment requirements:** The Cloudflare Access app must protect
`app.katcha.stream`, forward its JWT assertion, and the deployed **API release**
must support Access identity verification. Set the team domain, application AUD,
application domain, and an explicit mapped email/principal with appropriate
scopes in the protected production environment. A Cloudflare login alone does
not grant Katcha API privileges. Do not remove the existing legacy operator
credential until the browser session and channel permissions are verified.

Release publication to GHCR does **not** update the running OCI primary. Verify
the exact deployed SHA, apply a reviewed production rollout, then check
`/v1/control/session`, `/v1/channels`, and the browser connection state for
all five workspaces. Do not authorize production promotion merely because a
branch or PR was merged.
