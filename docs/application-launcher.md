# Katcha application launcher

From the repository on Ubuntu/WSL, open **Katcha.sh** (or run `./Katcha.sh`).
From Windows, double-click **Katcha.cmd**. Windows checkouts use the default WSL
distribution; `\\wsl.localhost\Ubuntu\home\...` and `\\wsl$\Ubuntu\home\...` checkouts
automatically select the matching distribution and Linux directory.
To add a desktop shortcut, run `powershell.exe -NoProfile -File .\Katcha.ps1 -InstallShortcut`
from the checkout in PowerShell. Local PowerShell execution policy must permit this script;
the launcher does not bypass organizational policy.

The launch console opens at **http://localhost:8765**. On first use it opens in an idle state so
**Start Katcha** is immediately clickable. Clicking Start enters the workspace shell immediately.
The launcher then brings up the lightweight database/API control plane first; production workers,
Temporal, object storage, analysis, and rendering warm behind the workspace instead of blocking it.
No virtualenv,
credential exports, development web server, or manual port forwarding is needed.
Python 3.11+ and Docker Desktop with WSL integration (Compose 2.24.4+) must be installed.
The launcher reports missing Docker in its diagnostics; it does not install system software.

## First launch

1. Start Docker Desktop. Launch Katcha. The console opens without starting services automatically.
2. Open **Connections & setup**. Existing `.env` settings are retained. If `.env`
   is absent, the example is copied; missing control and encryption keys are generated once.
3. Enter provider credentials and choose **Live** AI explicitly when ready to use paid services.
   Fixture mode remains clearly labeled and does not make live provider calls.
4. Save settings, then click **Start Katcha** to apply them. Katcha opens immediately and reports
   **WARMING** until the database/API control plane is ready. Heavy automation images and services
   continue warming afterward; their status remains visible from the launch console. The gateway
   supplies control authentication without exposing its token to the browser.
5. For YouTube, save OAuth client details and register the displayed callback URL in
   your Google OAuth application. Restart, then click **Connect YouTube** to authorize.

This local application trusts users of the computer. Keep it bound to loopback.
It is not a multi-user internet hosting gateway. API port 8000 remains on loopback
for the existing OAuth callback; other internal service ports are not published.
The application Compose overlay uses Docker's documented `!reset`/`!override` merge
support: https://docs.docker.com/reference/compose-file/merge/.

## Rendering and configuration

Local rendering works without AWS. Lambda mode automatically selects the AWS renderer
overlay and the Roles Anywhere overlay when its helper path is saved. The launcher
uses the current user's UID/GID, saved profile and existing host `~/.aws` configuration.
It never provisions AWS resources or invents permissions. Use the existing guarded
AWS deployment scripts for the initial infrastructure setup. Expired interactive AWS
sessions still require provider reauthentication; their failures appear in service logs.

All existing provider settings remain supported through `.env`. The console exposes
common AI, YouTube and Lambda settings; Roles Anywhere paths and advanced discovery
settings retain their existing configuration workflow. Empty password fields keep saved
values. To remove a credential, clear the value in `.env` and restart.

Saved settings override inherited `KATCHA_*`, `AWS_*`, `REMOTION_*` and `COMPOSE_*`
shell values. Compose uses the existing **katcha** project and volumes. If the old
stack used a different project name, migrate deliberately before using this launcher;
it does not automatically discover or move another project's database.

## Lifecycle and diagnostics

- **Start** saves persistent run intent and validates Compose, then performs a two-phase boot.
  Phase 1 builds or reuses the lightweight control-plane image and starts only Postgres, migrations,
  and the API. As soon as `/v1/health/workspace` passes, the workspace is usable. Phase 2 prepares
  storage, orchestration, ingestion, analysis, rendering, and production workers in the background.
  Repeated clicks are serialized.
- **Stop services** uses Compose stop; it does not delete containers, volumes or media.
- Closing or disconnecting the browser does not restart Katcha; the supervisor keeps the
  current startup/runtime state and the browser picks it back up when it reconnects.
- If the launcher process itself is restarted while Katcha containers still exist, it
  discovers and reattaches to that Compose stack without rebuilding or relaunching it.
  Running services resume health/log monitoring; stopped containers remain stopped.
- If Docker or the API is temporarily unavailable during reattachment, the launcher enters
  a recoverable degraded state and continues probing instead of resetting to a fresh launch.
  Docker's restart policy restarts long-running services when Docker returns.
- Dependencies are probed every ten seconds while the launcher runs. Worker process
  status is checked, but this does not prove that every workflow is making progress.
- **Export diagnostics** downloads retained JSONL events, including prior launcher
  sessions, timestamps, component, phase, errors, traceback and recovery guidance.
- Logs live at `.local/runtime/events.jsonl`, rotating at 5 MB with five backups.
  Docker logs rotate separately at 10 MB × three files per service. The launcher
  captures recent logs on restart and follows subsequent output while open.
- Known configured secrets, bearer tokens, connection passwords and common signed
  query credentials are redacted. Redaction is best effort: review diagnostics before
  sharing, especially third-party payloads. The export never includes `.env`.
- Startup timeouts, configuration failures and API 5xx/gateway errors are recorded;
  uncaught browser errors are recorded from launcher-connected workspaces. Handled
  business validation errors remain in their normal UI/workflow records.

`./Katcha.sh` resumes previously requested operation automatically. After an explicit Stop,
it leaves services stopped; on first use it may reattach to an already-running stack. `./Katcha.sh --auto-start` explicitly requests startup.
`./Katcha.sh --no-browser` opens the launcher without opening a browser.
If port 8765 is occupied, no second supervisor starts; the terminal explains how to
open the existing console. If port 8000 conflicts, Compose fails and records the error.

## Validation

Run `python -m pytest launcher/tests -q` and `ruff check launcher`.
The launcher CI job also validates the merged application Compose configuration and
runs browser interactions. Full first-run acceptance on Windows/WSL with Docker,
real provider authorization and an actual production video remains a separate gate.

## Unattended operation and fast restart

After Start, the supervisor remembers that Katcha should run. Failed startup retries
with exponential delays (10 seconds up to five minutes); explicit Stop clears that
intent. Missing/exited services are reconciled with Compose; healthy running containers
are not force-recreated. Unhealthy running services remain visible for diagnosis rather
than being repeatedly killed. Provider revocation and expired interactive AWS login
still require authentication; this does not turn interactive credentials into service credentials.

On the hosting Ubuntu/WSL machine, install the background supervisor once:

```bash
cd ~/src/katcha
bash scripts/install_launcher_service.sh
```

Open http://localhost:8765 and click Start once. The systemd user service restarts the
supervisor after failure. To keep the user service running after logout, an administrator
must enable lingering for that user (`sudo loginctl enable-linger "$USER"`). WSL requires
systemd enabled. Windows must keep WSL and Docker Desktop running; this installer does
not configure Windows boot or prevent sleep. A continuously powered Linux host is the
appropriate deployment when discovery must run independently of a personal computer.

To undo service installation without deleting data:
`systemctl --user disable --now katcha-launcher.service`. Use **Stop services** first
if the containers should also stop.

The launcher keeps separate workspace and full-automation build fingerprints. Renderer-only
changes no longer invalidate the blocking workspace image, and unchanged launches verify that
expected Katcha-built images still exist before skipping rebuilds. Shared control-plane and AI
images remove duplicate service builds, while BuildKit package caches accelerate unavoidable
rebuilds. Credentials are never hashed or stored in fingerprints and failed builds never update
them. To force a full background rebuild, remove `.local/runtime/build-fingerprint`; to force the
control plane too, also remove `.local/runtime/workspace-build-fingerprint`. Startup acceptance
reports workspace-ready and full-automation timings separately.

Discovery workers consume configured topic-watch schedules. Keeping workers running
does not create interests or schedules automatically and does not prove ingestion progress.
