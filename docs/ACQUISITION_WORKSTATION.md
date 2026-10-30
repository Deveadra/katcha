# Persistent workstation acquisition

The workstation can run the same channel-scoped worker through its systemd user
manager. This profile uses the public HTTPS control origin through Cloudflare
Access, eliminating the temporary Bastion/SSH/TLS-gateway dependency. Downloads
and signed multipart uploads still use the workstation's own outbound connection.
It does not provision a machine, change Access policies, or turn on automatic
remote dispatch.

## Prepare the runtime

Use an accepted release with matching primary API/ingestion code and schema.
Install its acquisition dependencies, Python 3.12, Node 22 and FFmpeg under
`~/.local/share/katcha-acquisition`, with the runtime interpreter at
`~/.local/share/katcha-acquisition/.venv/bin/python`. Install the reviewed release
as a wheel or from a fixed checkout. Do not point an unattended service at a
checkout that other agents continue changing.

Create these user-owned private directories with mode 0700:

- `~/.config/katcha-acquisition/credentials`
- `~/.local/state/katcha-acquisition`

Copy `deploy/acquisition/workstation.env.example` to
`~/.config/katcha-acquisition/worker.env` (0600), then set the explicit channel UUIDs
and trusted HTTPS API origin. Keep remote automatic dispatch off during admission.

## Credentials

The credential directory requires `worker-token`, plus `cf-access-id` and
`cf-access-secret` when using Cloudflare Access. Each must be a regular, nonempty
file, owned by the service user, mode 0600.
Use a dedicated Katcha principal with only `acquisition:work` and the selected
channel UUIDs. Preserve existing operator access. The worker never needs bucket,
database, OCI, or operator credentials.

A human operator creates or explicitly approves reuse of the Cloudflare service
credential and permits it in the application's Service Auth policy. Creating a
file or passing a mount check does not prove Access authentication. Never put
secret values in chat, Git, unit files, command arguments, or the environment
file. The service loads private credential snapshots using systemd LoadCredential.

## Activate and verify

Review and copy `deploy/acquisition/katcha-acquisition-workstation.service` into
`~/.config/systemd/user/`. For Cloudflare Access, also copy
`deploy/acquisition/workstation-access.conf` into
`~/.config/systemd/user/katcha-acquisition-workstation.service.d/`. Both Access
credential files are required together. The base unit also supports an authorized
private HTTPS control origin without the Access drop-in. For a private CA, install
`workstation-private-ca.conf` in that same drop-in directory and put its CA bundle
at `~/.config/katcha-acquisition/credentials/control-ca.pem`. TLS and hostname
verification remain enabled; the CA applies only to control traffic.

Validate before starting:

```bash
systemd-analyze --user verify ~/.config/systemd/user/katcha-acquisition-workstation.service
systemctl --user daemon-reload
```

First stop the temporary worker after its current operation finishes. Reuse its
private journal directory by moving it only while the worker is stopped, or set
`KATCHA_ACQUISITION_STATE_DIR` to that existing absolute directory in worker.env.
Do not start two workers against different journals for the same host identity.
The worker also takes a nonblocking lock on its journal directory.

```bash
systemctl --user enable --now katcha-acquisition-workstation.service
systemctl --user is-active katcha-acquisition-workstation.service
journalctl --user -u katcha-acquisition-workstation.service -n 30 --no-pager
```

A running service alone is not acceptance. First run `python -m katcha.acquisition.worker --check-auth` from the installed worker environment, with the same systemd credential mounts, and investigate any sanitized failure code using [Access authentication and 403 diagnosis](ACQUISITION_ACCESS_AUTH.md). Verify the real narrow token reaches
the channel API through Access, other channels return 403, operator validation
returns 403, and an explicitly authorized job completes primary-side media
validation. Test a service restart with the temporary SSH control path stopped;
verify durable job identity and canonical media, not only process exit status.

For operation after logout, the operator can enable user lingering with
`loginctl enable-linger "$USER"` and verify `loginctl show-user "$USER" -p Linger`.
This affects the user's service manager; review the machine's existing policy
before changing it. On WSL, Windows must also start the distribution. User
lingering cannot start a powered-off computer, start WSL from Windows, or prevent
Windows sleep. A reboot test and observed uptime are separate acceptance checks.

The service restarts after crashes and network recovery; unavailable control
leaves durable state intact. A host restart with an ambiguous provider operation
still requires the existing reconciliation policy. Restart does not grant new
permission or authorize an uncertain download replay.

To stop new work without deleting journals or secrets:

```bash
systemctl --user disable --now katcha-acquisition-workstation.service
```

Keep the temporary route available until direct authentication, service restart,
and an actual handoff have passed. Do not copy OCI-side service credentials to a
second machine without explicit operator approval of that additional location.

## Human verification on this workstation

The worker can now pause an authentication challenge and accept operator browser
commands through its existing authenticated outbound control connection. Enable
`KATCHA_ACQUISITION_HUMAN_RECOVERY_ENABLED=true` in the workstation environment
only after installing the release with its `browser` extra and its matching
Chromium runtime:

```sh
.venv/bin/python -m pip install '.[browser]'
.venv/bin/python -m playwright install chromium
```

Use the same immutable release as the API. The API must also enable human
recovery and have its credential encryption key configured; Telegram delivery
requires its configured recipient, admitted channels and authenticated app origin.
The existing supervised service keeps the browser on the acquisition host and
opens no inbound port. Do not point this recovery flow at the separate goal
browser: the downloader must use this host's session and network.

When a source requires verification, the worker releases its media-processing
slot and waits without retrying the source. Open the recovery link in Katcha and
choose **Open verification**. Complete the provider's prompts yourself in that
browser view, then choose **Check access and continue**. That action authorizes
one new download attempt; it does not declare the CAPTCHA solved or the media
verified. The normal independent hash/probe and channel handoff still determine
whether acquisition succeeded.

The worker exports only this job's approved-domain cookies to a private 0600
file, uses the browser's User-Agent for the retry, and removes the file after the
attempt. Cookies never enter Telegram, API payloads or the job journal. A browser
restart loses the live session; reopen verification instead of repeating an
uncertain form submission. Lost control responses reuse the saved execution
identity. An interrupted extraction is quarantined rather than replayed.

Browser actions and waiting requests expire. A cancelled or expired request
closes its session when the worker next observes that state. The browser also
has its own idle and absolute expiry. Runtime, network and provider acceptance
must be checked on the intended workstation; the offline regression tests do
not prove a successful YouTube sign-in or download.
