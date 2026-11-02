# Acquisition Cloudflare Access 403: unattended recovery contract

## What is known

The acquisition worker uses a dedicated Katcha `acquisition:work` bearer principal plus
optional Cloudflare Access service-token headers. The checked-in Terraform creates
an operator-email-only Access application at the site root and a **separate, more
specific `/v1/*` Bypass application**. The API itself still enforces scope and
channel access. A 403 alone does not prove Cloudflare rejected a request.

The installed credential metadata/mount checker does **not** validate either
credential against the live control plane. Merely mounting `cf-access-id` and
`cf-access-secret` cannot clear the activation gate.

## Read-only diagnostic, no job claims or secrets in output

From the acquisition worker's configured environment and private credential mounts,
using the exact accepted worker image, run:

```sh
python -m katcha.acquisition.worker --check-auth
```

The check calls only `GET /v1/health/live` and
`GET /v1/channels/{configured-channel-uuid}/acquisition-jobs?status=queued&limit=1`
for every configured channel. It requires **Katcha's** JSON health identity
and a valid list response with the real scoped bearer principal. It cannot
claim, download, upload, publish, or approve media. Exit 0 is a successful
read-only authorization check, not a proven media handoff or activation.

Failure codes:

| Code | Meaning / next automation step |
| --- | --- |
| `edge_or_unclassified_http_401/403` | Non-Katcha JSON denial, possibly Cloudflare Access/WAF; correlate Access logs and the **exact request path** with the Access application matched |
| `katcha_principal_http_401/403` | FastAPI-style JSON detail; likely bearer principal missing, expired, or incorrectly scoped; check principal registry and allowed channel UUIDs |
| `edge_or_unclassified_redirect` | Redirect denied; check Access application/policy, not a browser login |
| `control_non_json_response` | A proxy/login/other non-JSON response where Katcha JSON was required |
| `control_health_unexpected_response` | HTTP success came from something other than the expected Katcha health response |
| `worker_principal_unexpected_response` | API returned the wrong shape for the channel-scoped list |
| `control_transport_error` | Network/TLS/timeout issue; alert separately from authorization |

No code includes request or response bodies, redirect URLs, bearer credentials, or
Cloudflare client secret in diagnostic messages. A `cf-ray` header does **not**
distinguish an edge denial from an API denial because proxied API responses can
also have a `cf-ray`.

## Cloudflare policy investigation and correction

1. Inspect the **live** Zero Trust application matching the failed hostname +
   path. For `/v1/*`, compare it to the checked-in bypass application. A
   different application match, wrong hostname/path, WAF denial, or stale IaC
   is configuration drift. Do not assume the root operator application handled
   the request.
2. If an Access-protected path is intentionally used for service-token
   authentication, it must have a **Service Auth** policy whose Include/Service
   Token selector names the installed credential. An operator email Allow rule
   does not authorize a service token, particularly under strict service-token
   authentication. Maintain the human operator Allow policy separately.
3. Verify the service token is enabled, not expired, and bound to the correct
   Zero Trust account. Compare nonsecret Client ID and token identity, never
   copy its secret into a ticket or logs. Inspect Access authentication logs for
   disabled, expired, secret mismatch, or token-not-authorized reasons.
4. Prefer preserving the intended `/v1/*` bearer-only API route if it is the
   live design; do **not** casually replace all API routes with Service Auth.
   Recovery automation, OAuth callbacks and machine clients depend on the
   existing path contract. Never add an `Everyone` Allow on the operator app
   or bypass the operator UI just to silence a 403.
5. Reconcile an approved change through Terraform, re-read the live policy, and
   re-run the check in the actual worker runtime. An applied IaC plan alone is
   not live acceptance.

## Unattended acceptance

- Confirm read-only auth success after worker process and host restart with no
  interactive Access session; repeat after intentional expiry/rotation fixture.
- Validate that the worker token cannot access a different channel or operator
  mutation, and that no Access token alone can access private Katcha operations.
- Monitor the preflight at bounded intervals, classify changes in failure stage,
  and send a sanitized incident with a known recovery action to operations and
  Telegram. Do not retry permanent 403 denials every five seconds.
- Track service-token expiry in advance and renew through least-privilege
  automation where authorized. Secret rotation must create a new secret with
  overlap, distribute via a private secret store, restart/reload credential
  consumers, and test the new secret **before** revoking the old one.
- Keep worker dispatch disabled until exact-release CI, scoped Katcha principal,
  edge auth preflight, backup/restore, and an actual verified media handoff pass.
  A successful preflight alone does not authorize or activate production.

Cloudflare references:
- https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/
- https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/
- https://developers.cloudflare.com/cloudflare-one/access-controls/policies/
