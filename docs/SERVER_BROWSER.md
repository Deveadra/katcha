# Server browser

Katcha AI can use a real Chromium browser to open public HTTPS pages, execute page
JavaScript, read rendered content, follow links, complete forms, click/drag viewport
coordinates, hover, type into focused controls, press keys, use tabs and
retain sign-in cookies during a goal. Operators can inspect and interact with the
same session through the goal's browser panel. The browser runs on the server;
signing in to a browser on your own computer does not sign in this session.

## Run locally

Use the standard Compose stack with `docker-compose.intelligence.yml` for the
goal worker. The `browser` service builds `Dockerfile.browser`
with the Python `browser` extra and the matching Chromium revision. The API and
intelligence worker connect to `http://browser:8010`. No browser port is published
on the host. `docker compose --env-file .env.example build browser` builds the
runtime without starting providers or production work.

For a Python development environment outside Compose:

```sh
python -m pip install -e '.[dev,browser]'
python -m playwright install --with-deps chromium
```

Set `KATCHA_BROWSER_SERVICE_URL` on the API and goal worker. Give those processes
(the `intelligence-worker` that owns `CommandGoalWorkflow`) and the browser
service the same `KATCHA_BROWSER_SERVICE_TOKEN` of at least 32
characters. Start one browser-service process using:

```sh
uvicorn katcha.services.server_browser:app --host 127.0.0.1 --port 8010 --workers 1
```

One process owns the in-memory sessions. Multiple independent browser replicas
require sticky routing; randomly balancing requests between replicas loses
session continuity. `/health` reports readiness. The authenticated `/execute`
endpoint accepts `{ "scope": ["actor", "channel", "goal"], "arguments": {...} }`;
only the API and worker should call this internal endpoint. The API derives scope
from the authenticated goal rather than accepting a client-selected identity.

## Sign-in and recovery

Open the goal's browser panel and navigate to the target site. Use that panel for
sign-in forms and one-time verification codes so credentials are not included in
AI prompts or goal tool receipts. The operator endpoint is
`/v1/ai/goals/{goal_id}/browser`. Refresh the page view to see the current controls;
element references become stale after interaction and must be refreshed. Pointer
and focused-keyboard actions must include the returned `snapshot_id`; outdated
views are rejected when another AI or operator action changes the session. The
operator panel attaches this identity automatically.

Sessions are separate for each actor, channel and goal. Cookies and page state
stay in memory and expire after 15 idle minutes or a service restart. Closing a
session clears its context. If a submission times out or connectivity is lost,
refresh and inspect the result before retrying: the site might already have
accepted it. There is no automatic replay of uncertain submissions. Sign in again
when a session has expired. An unavailable browser returns a recovery message;
configuration alone does not prove a live session works.

## Network and isolation

Navigation and subresources use a local CONNECT proxy that resolves public HTTPS
destinations and connects to the validated IP address. Private, loopback,
link-local and other non-public destinations are rejected, including redirects
and subresources. HTTP, local files and arbitrary local network access are not
supported. Downloads are disabled; the browser does not grant media usage rights
or establish scene verification. CAPTCHAs, site restrictions, hardware security
keys and providers that reject automated browsers can still require operator
intervention or a different source.

The dedicated container runs as UID 10001, with no host mounts, provider keys,
application database credentials or shared user profile. Compose configures a
read-only root filesystem, a writable ephemeral `/tmp`, private shared memory,
an init process, dropped capabilities and `no-new-privileges`. Production also
bounds memory, CPU and process count. The browser service receives only its
internal service token. Rotate that token independently of provider credentials.
Keep port 8010 private; use encrypted transport if RPC crosses trusted host or
container network boundaries.

Playwright's Chromium sandbox is disabled by its default launch configuration.
Running the process as a non-root user does not enable that sandbox. These
container restrictions and the destination proxy are defense in depth, not a
complete sandbox against browser exploits. A compromised Chromium process still
shares its container and network with the proxy service. Deploy stronger runtime
isolation and external egress controls when the threat model requires them; do
not enable privileged containers or mount host credentials to fix launch errors.
See the official [Playwright Docker guidance](https://playwright.dev/python/docs/docker)
for the distinction between user identity and Chromium sandbox configuration.

## CI and release

The `Server browser Chromium acceptance` CI job installs the Python package and
matching browser, then runs `tests/test_server_browser.py` with
`KATCHA_TEST_REAL_BROWSER=1`. This exercises a real browser against a controlled
HTTPS fixture. It proves browser behavior and isolation for that fixture; it does
not prove a production login, provider access, playback or media rights.

The separate `katcha-browser` image is published for the exact accepted main SHA
with the other runtime images. Release eligibility requires all seven images and
anonymous pull verification. The production deployer stages the browser image,
tracks the service alongside existing workers and preserves immutable release
pins. Generate a unique `KATCHA_BROWSER_SERVICE_TOKEN` in the production env file
before rollout. The example token is a placeholder, not an operator secret.

Browser startup problems usually mean the matching Chromium binary or system
libraries are missing. Rebuild the image rather than installing into a running
container. `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` makes the build-installed
browser available to the non-root runtime. Host Python installs must rerun
`python -m playwright install --with-deps chromium` after upgrading Playwright.
