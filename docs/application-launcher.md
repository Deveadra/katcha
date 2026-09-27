# Katcha application launcher

From the repository on Ubuntu/WSL, open **Katcha.sh** (or run `./Katcha.sh`).
From a Windows checkout, double-click **Katcha.cmd** using the default WSL distribution.
For a repository inside WSL, launch `~/src/katcha/Katcha.sh` within that distribution;
Windows UNC paths are not a supported batch-launch location.

The launch console opens at **http://localhost:8765**. It starts the full stack,
checks readiness, and enables **Open workspace** when healthy. No virtualenv,
credential exports, development web server, or manual port forwarding is needed.
Python 3.11+ and Docker Desktop with WSL integration (Compose 2.24.4+) must be installed.
The launcher reports missing Docker in its diagnostics; it does not install system software.

## First launch

1. Start Docker Desktop. Launch Katcha. The first image build may take several minutes.
2. Open **Connections & setup**. Existing `.env` settings are retained. If `.env`
   is absent, the example is copied; missing control and encryption keys are generated once.
3. Enter provider credentials and choose **Live** AI explicitly when ready to use paid services.
   Fixture mode remains clearly labeled and does not make live provider calls.
4. Save settings, then click **Start Katcha** to apply them. Open the workspace.
   The gateway supplies control authentication without exposing its token to the browser.
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

- **Start** validates Compose, builds images, runs migrations/bucket initialization,
  starts all workers and waits for health. Repeated clicks are serialized.
- **Stop services** uses Compose stop; it does not delete containers, volumes or media.
- Closing the launcher leaves services running. Reopen it to restore monitoring.
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

`./Katcha.sh --no-start` opens setup without starting Docker services.
`./Katcha.sh --no-browser` starts without opening a browser.
If port 8765 is occupied, no second supervisor starts; the terminal explains how to
open the existing console. If port 8000 conflicts, Compose fails and records the error.

## Validation

Run `python -m pytest launcher/tests -q` and `ruff check launcher`.
The launcher CI job also validates the merged application Compose configuration and
runs browser interactions. Full first-run acceptance on Windows/WSL with Docker,
real provider authorization and an actual production video remains a separate gate.
