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
