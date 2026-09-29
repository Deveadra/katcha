# Control-plane live acceptance

This is the final operational acceptance for the Katcha AI external-control contract.
It is intentionally narrower than the media-production acceptance in
`LIVE_ACCEPTANCE.md`.

The goal is to prove, with real named credentials, that the human operator and
Aerith are separate control principals and that Aerith can complete one safe,
auditable Katcha AI command lifecycle end to end.

The acceptance does **not** create, upload, schedule, or publish media. Its only
mutation is a channel-intelligence refresh.

## What a pass proves

A passing run proves all of the following against the running stack:

- the operator and Aerith authenticate with distinct named credentials;
- `GET /v1/control/session` negotiates the expected control contract;
- Aerith is constrained to the intended channel unless an explicit wildcard
  override is supplied;
- Aerith has the scopes required by both the route contract and the command
  endpoint;
- the AI surface is in live-provider mode by default;
- Aerith can establish and resume a principal-bound, channel-bound event cursor;
- `POST /v1/ai/command` creates a server-issued action proposal without executing it;
- the proposal cannot execute until Aerith explicitly sends
  `{"confirmed": true}`;
- the action is attributed to `control-principal:aerith`;
- the Temporal-backed intelligence refresh reaches its terminal lifecycle;
- the event stream exposes the correlated proposal, confirmation, execution,
  workflow-start, and workflow-complete events;
- Aerith can acknowledge the terminal event and advance its cursor.

## Acceptance scopes

Use a wildcard operator principal for the human/operator surface and a
least-privilege Aerith principal for this first acceptance.

Aerith needs all of these scopes:

```text
ai:read
ai:command
channels:read
events:read
events:ack
intelligence:write
```

Both `ai:command` and `ai:read` are intentional. The global named-principal
route guard requires `ai:command` for `/v1/ai/command`, while the command
endpoint itself requires `ai:read`.

Do not grant `production:create`, `render:recover`, or `discovery:write`
for this first acceptance. They are not needed to prove the control plane.

## 1. Start from a clean repository state

From the Katcha repository:

```bash
cd ~/src/katcha
git status --short
```

Do not overwrite local work. Back up the current environment file before editing
credentials:

```bash
cp .env ".env.before-control-acceptance.$(date +%Y%m%d-%H%M%S)"
```

The backup contains secrets. Keep it local and delete it securely after the
acceptance/rollback window.

## 2. Select the real target channel

Set the channel profile ID that Aerith is allowed to operate during acceptance:

```bash
export KATCHA_ACCEPTANCE_CHANNEL_PROFILE_ID="<channel-profile-id>"
```

Use the actual Katcha channel profile UUID, not a YouTube channel ID.

## 3. Generate distinct real credentials

Generate two high-entropy credentials directly into shell variables. These
commands do not print the generated values:

```bash
export KATCHA_ACCEPTANCE_OPERATOR_TOKEN="$(
  python -c 'import secrets; print(secrets.token_urlsafe(48))'
)"
export KATCHA_ACCEPTANCE_AERITH_TOKEN="$(
  python -c 'import secrets; print(secrets.token_urlsafe(48))'
)"
```

Verify only that they exist and differ:

```bash
python - <<'PY'
import os

operator = os.environ["KATCHA_ACCEPTANCE_OPERATOR_TOKEN"]
aerith = os.environ["KATCHA_ACCEPTANCE_AERITH_TOKEN"]

assert len(operator) >= 16
assert len(aerith) >= 16
assert operator != aerith
print("PASS: operator and Aerith credentials are present and distinct")
PY
```

Never paste either token into chat, a GitHub issue, a PR, terminal screenshots,
or committed files.

## 4. Configure the named-principal registry

The named-principal registry becomes authoritative when
`KATCHA_CONTROL_PRINCIPALS` is non-empty. The legacy
`KATCHA_CONTROL_API_TOKEN` by itself will no longer authenticate.

Katcha's operator surfaces may still read `KATCHA_CONTROL_API_TOKEN` as the
token they send, so keep that variable synchronized with the `operator-ui`
credential.

Run the following in the same shell that contains the two generated credentials:

```bash
python - <<'PY'
import json
import os
from pathlib import Path

path = Path(".env")
if not path.exists():
    raise SystemExit(".env does not exist")

channel_id = os.environ["KATCHA_ACCEPTANCE_CHANNEL_PROFILE_ID"].strip()
operator_token = os.environ["KATCHA_ACCEPTANCE_OPERATOR_TOKEN"]
aerith_token = os.environ["KATCHA_ACCEPTANCE_AERITH_TOKEN"]

registry = [
    {
        "name": "operator-ui",
        "credentials": [
            {
                "id": "operator-live-acceptance",
                "token": operator_token,
            }
        ],
        "scopes": ["*"],
        "channel_profile_ids": ["*"],
    },
    {
        "name": "aerith",
        "credentials": [
            {
                "id": "aerith-live-acceptance",
                "token": aerith_token,
            }
        ],
        "scopes": [
            "ai:read",
            "ai:command",
            "channels:read",
            "events:read",
            "events:ack",
            "intelligence:write",
        ],
        "channel_profile_ids": [channel_id],
    },
]

updates = {
    "KATCHA_CONTROL_API_TOKEN": operator_token,
    "KATCHA_CONTROL_PRINCIPALS": json.dumps(
        registry,
        separators=(",", ":"),
    ),
}

lines = path.read_text().splitlines()
seen = set()
output = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in updates:
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        output.append(line)

for key, value in updates.items():
    if key not in seen:
        output.append(f"{key}={value}")

path.write_text("\n".join(output) + "\n")
print("PASS: wrote named control principals without printing credential values")
PY
```

The operator and Aerith tokens are intentionally different. The operator remains
wildcard-capable; Aerith is limited to the one acceptance channel.

## 5. Enable real Katcha AI

For the real operational gate, Katcha must be configured for live AI:

```text
KATCHA_AI_ENABLED=true
KATCHA_AI_EXECUTION_MODE=live
```

At least one configured AI provider must be available. Keep normal budget controls
enabled; the acceptance does not bypass them.

Restart/relaunch Katcha so the new credential registry and AI mode are loaded.
Use the normal launcher rather than starting ad hoc duplicate service stacks.

## 6. Verify health before mutating anything

```bash
curl --fail --silent http://127.0.0.1:8000/v1/health/ready >/dev/null   && echo "PASS: Katcha API is ready"
```

If readiness fails, stop here and diagnose the stack. Do not run the acceptance
against a partially healthy Katcha instance.

## 7. Run the controlled lifecycle

The acceptance runner uses the two shell credentials but never prints them:

```bash
python scripts/control_plane_live_acceptance.py \
  --channel-profile-id "$KATCHA_ACCEPTANCE_CHANNEL_PROFILE_ID"
```

The runner performs this exact sequence:

1. operator `/v1/control/session` handshake;
2. Aerith `/v1/control/session` handshake;
3. verification that actors and credential fingerprints are distinct;
4. verification of Aerith scopes and channel boundary;
5. `/v1/ai/readiness` live-provider check;
6. creation/baselining of the dedicated Aerith event cursor;
7. real Katcha AI request for yesterday's performance/editing advice;
8. verification that Katcha proposes exactly one
   `refresh_channel_intelligence` action;
9. explicit Aerith confirmation of that exact server-issued proposal;
10. polling of `/v1/ai/actions/{proposal_id}/activity` until the workflow settles;
11. verification of the correlated domain-event lifecycle;
12. acknowledgement of the terminal `workflow_completed` event.

A successful result is JSON with:

```json
{
  "status": "passed",
  "aerith": {
    "actor": "control-principal:aerith",
    "channel_scoped": true
  },
  "activity_state": "completed",
  "ai_live": true
}
```

The result also includes non-secret request, thread, proposal, workflow, event,
credential-label, and cursor identifiers for audit evidence.

## Diagnostic-only fixture run

If the control plane must be tested before a live AI provider is available, the
runner can explicitly permit fixture mode:

```bash
python scripts/control_plane_live_acceptance.py \
  --channel-profile-id "$KATCHA_ACCEPTANCE_CHANNEL_PROFILE_ID" \
  --allow-fixture-ai
```

That is a diagnostic result, not the final real Katcha AI operational acceptance.

## Explicit wildcard Aerith override

The runner rejects wildcard Aerith channel access by default. If a later
deployment intentionally gives Aerith all-channel authority, that policy must be
made explicit during acceptance:

```bash
python scripts/control_plane_live_acceptance.py \
  --channel-profile-id "$KATCHA_ACCEPTANCE_CHANNEL_PROFILE_ID" \
  --allow-aerith-all-channels
```

Do not use this override merely to work around a bad channel allowlist.

## Failure interpretation

The runner is fail-closed. A failure means the acceptance is incomplete, not that
the harness should be weakened.

Common failures have direct meanings:

- **401 on session** — credential registry/token mismatch or Katcha was not reloaded;
- **missing `ai:command` / `ai:read`** — Aerith cannot use the Katcha AI command route;
- **channel authorization failure** — the Aerith allowlist does not contain the target channel;
- **AI readiness failure** — live AI/provider configuration is not ready;
- **unexpected action type** — do not execute it; inspect Katcha AI planning;
- **workflow failed/not settled** — inspect the action activity and Temporal/service logs;
- **missing lifecycle events** — command/event correlation is incomplete and must be fixed;
- **cursor acknowledgement failure** — do not treat event consumption as production-ready.

## After acceptance

Keep the stable principal names (`operator-ui` and `aerith`) and rotate
credential IDs/tokens using the overlap procedure in
`CONTROL_PLANE_AUTHENTICATION.md`. Stable principal identity preserves Aerith's
event cursor binding across credential rotation.

After the real pass, the next expansion should be deliberate: grant only the
additional action scopes Aerith actually needs (`production:create`,
`render:recover`, `discovery:write`, and so on) and acceptance-test each
higher-risk capability before enabling unattended use.
