"""Local Katcha supervisor. Python standard library only; no virtualenv required."""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import http.client
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "KATCHA_OPENAI_API_KEY",
    "KATCHA_GEMINI_API_KEY",
    "KATCHA_AI_EXECUTION_MODE",
    "KATCHA_YOUTUBE_CLIENT_ID",
    "KATCHA_YOUTUBE_CLIENT_SECRET",
    "KATCHA_YOUTUBE_DATA_API_KEY",
    "KATCHA_RENDER_BACKEND",
    "KATCHA_AWS_PROFILE",
    "KATCHA_AWS_EXPECTED_ACCOUNT_ID",
    "KATCHA_REMOTION_LAMBDA_REGION",
    "KATCHA_REMOTION_LAMBDA_FUNCTION_NAME",
    "KATCHA_REMOTION_LAMBDA_SERVE_URL",
    "KATCHA_REMOTION_STAGING_BUCKET",
}
SENSITIVE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
CORE_REQUIRED = {"postgres", "temporal", "minio", "api"}
BACKGROUND_SERVICES = (
    "worker",
    "analysis-worker",
    "renderer",
    "production-worker",
    "longform-worker",
    "publishing-worker",
    "discovery-worker",
    "trends-worker",
    "intelligence-worker",
    "temporal-ui",
)
BACKGROUND_REQUIRED = set(BACKGROUND_SERVICES) - {"temporal-ui"}
ONE_SHOT_SERVICES = {"migrate", "minio-init"}
CORE_BUILD_SERVICES = ("api", "migrate", "minio-init", "minio")
MISSING_IMAGE = re.compile(
    r"no such image|pull access denied|not found|unable to get image|does not exist",
    re.I,
)


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


class Runtime:
    def __init__(self, root=ROOT):
        self.root = root
        self.directory = root / ".local" / "runtime"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.env_path = root / ".env"
        self.session = str(uuid.uuid4())
        self.events = collections.deque(maxlen=300)
        self.events_lock = threading.Lock()
        self.lock = threading.Lock()
        self.phase = "idle"
        self.stage = "idle"
        self.operation_started_at = None
        self.background_phase = "idle"
        self.background_error = ""
        self.background_thread = None
        self.known_secrets = set()
        self.services = []
        self.follow = None
        self.logger = logging.getLogger(self.session)
        self.logger.setLevel(logging.INFO)
        handler = logging.handlers.RotatingFileHandler(
            self.directory / "events.jsonl", maxBytes=5_000_000, backupCount=5
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)
        self.bootstrap()

    def bootstrap(self):
        if not self.env_path.exists():
            with open(self.env_path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                stream.write((self.root / ".env.example").read_text())
        values = read_env(self.env_path)
        additions = {}
        if not values.get("KATCHA_CREDENTIAL_ENCRYPTION_KEY"):
            additions["KATCHA_CREDENTIAL_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
                secrets.token_bytes(32)
            ).decode()
        if not values.get("KATCHA_CONTROL_API_TOKEN"):
            additions["KATCHA_CONTROL_API_TOKEN"] = secrets.token_urlsafe(32)
        if additions:
            self.save(additions)
        self.values = read_env(self.env_path)
        self.known_secrets.update(v for k, v in self.values.items() if v and SENSITIVE.search(k))

    def save(self, changes):
        # Keep comments and unrelated settings; never rotate an existing encryption key.
        lines = self.env_path.read_text().splitlines()
        found = set()
        for index, line in enumerate(lines):
            key = line.split("=", 1)[0].strip()
            if not line.lstrip().startswith("#") and key in changes:
                lines[index] = f"{key}='{changes[key]}'"
                found.add(key)
        lines.extend(f"{key}='{value}'" for key, value in changes.items() if key not in found)
        temporary = self.directory / "env.pending"
        with open(temporary, "w", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write("\n".join(lines) + "\n")
        temporary.replace(self.env_path)
        self.values = read_env(self.env_path)
        self.known_secrets.update(v for k, v in self.values.items() if v and SENSITIVE.search(k))

    def redact(self, message):
        text = str(message)
        for value in sorted(self.known_secrets, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        text = re.sub(r"(AKIA|ASIA)[A-Z0-9]{16}", "[REDACTED]", text)
        text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
        text = re.sub(
            r"(?i)((?:token|password|secret|api_key|signature|credential)[\w-]*[=:\s]+)[^\s&,]+",
            r"\1[REDACTED]",
            text,
        )
        text = re.sub(r"(https?://|postgresql[^:]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
        return text

    def event(self, level, component, message, **details):
        row = dict(
            schema="katcha.diagnostic.v1",
            time=dt.datetime.now(dt.UTC).isoformat(),
            session=self.session,
            level=level,
            component=component,
            phase=self.phase,
            message=self.redact(message),
            **{
                key: self.redact(value) if isinstance(value, str) else value
                for key, value in details.items()
            },
        )
        with self.events_lock:
            self.events.append(row)
        self.logger.info(json.dumps(row))

    def command(self):
        command = [
            "docker",
            "compose",
            "--project-name",
            "katcha",
            "--env-file",
            str(self.env_path),
        ]
        files = [
            "docker-compose.yml",
            "docker-compose.discovery.yml",
            "docker-compose.trends.yml",
            "docker-compose.intelligence.yml",
        ]
        if self.values.get("KATCHA_RENDER_BACKEND") == "lambda":
            files.append("docker-compose.aws-render.yml")
            if self.values.get("KATCHA_AWS_SIGNING_HELPER_PATH"):
                files.append("docker-compose.aws-roles-anywhere.yml")
        files.append("docker-compose.app.yml")
        for file in files:
            command.extend(["-f", str(self.root / file)])
        return command

    def environment(self):
        # Shell exports must not silently override the saved application configuration.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("KATCHA_", "COMPOSE_", "AWS_", "REMOTION_"))
        }
        env["KATCHA_ENV_FILE"] = str(self.env_path)
        env["KATCHA_HOST_UID"] = str(os.getuid())
        env["KATCHA_HOST_GID"] = str(os.getgid())
        return env

    def run(self, args, timeout=60, capture=False):
        process = subprocess.Popen(
            args,
            cwd=self.root,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines = queue.Queue()

        def read():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        output = []
        recent = collections.deque(maxlen=30)
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Command exceeded {timeout}s")
                try:
                    line = lines.get(timeout=min(remaining, 1))
                except queue.Empty:
                    continue
                if line is None:
                    break
                recent.append(line)
                if capture:
                    output.append(line)
                else:
                    self.event("info", "compose", line.rstrip())
            code = process.wait(timeout=max(1, deadline - time.monotonic()))
            if code:
                detail = "".join(output if capture else recent).strip()
                if capture:
                    self.event("error", "compose", detail)
                suffix = f"\n{detail}" if detail else ""
                raise RuntimeError(f"Command failed (exit {code}): {args[-1]}{suffix}")
            return "".join(output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def operate(self, action):
        if not self.lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._operate, args=(action,), daemon=True).start()
        return True

    def _operate(self, action):
        try:
            self.phase = "starting" if action == "start" else "stopping"
            self.stage = "preflight" if action == "start" else "stopping services"
            self.operation_started_at = time.monotonic()
            self.event("info", "launcher", f"{action} requested")
            self.values = read_env(self.env_path)
            self.known_secrets.update(
                v for k, v in self.values.items() if v and SENSITIVE.search(k)
            )
            version = self.run(["docker", "compose", "version", "--short"], capture=True)
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
            if not match or tuple(map(int, match.groups())) < (2, 24, 4):
                raise RuntimeError("Install Docker Compose 2.24.4 or newer.")
            self.run(["docker", "info", "--format", "{{.ServerVersion}}"])
            if action == "stop":
                self.background_phase = "stopping"
                self.run(self.command() + ["stop", "--timeout", "30"], timeout=120)
                self.phase = "stopped"
                self.stage = "stopped"
                self.background_phase = "stopped"
                return
            self.stage = "validating configuration"
            self.run(self.command() + ["config", "--quiet"])
            self.stage = "starting workspace"
            self.event(
                "info",
                "launcher",
                "Starting the workspace first; production engines will warm in the background.",
            )
            core_up = self.command() + [
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
                "--no-build",
                "api",
            ]
            try:
                self.run(core_up, timeout=240)
            except RuntimeError as exc:
                if not MISSING_IMAGE.search(str(exc)):
                    raise
                self.stage = "building workspace"
                self.event(
                    "info",
                    "launcher",
                    "Workspace images are not installed yet; building only the core application.",
                )
                self.run(
                    self.command() + ["build", *CORE_BUILD_SERVICES],
                    timeout=1800,
                )
                self.stage = "starting workspace"
                self.run(core_up, timeout=240)
            self.stage = "checking workspace readiness"
            self.start_logs()
            self.check()
            if self.phase != "ready":
                raise RuntimeError(
                    "Workspace services started but the API is not ready; inspect diagnostics."
                )
            self.stage = "ready"
            self.event(
                "info",
                "launcher",
                "Workspace is ready; production engines are warming in the background.",
            )
            self.background_phase = "warming"
            self.background_error = ""
            self.background_thread = threading.Thread(
                target=self._warm_background,
                daemon=True,
            )
            self.background_thread.start()
        except Exception:
            self.phase = "failed"
            self.stage = "failed"
            self.event(
                "error",
                "launcher",
                traceback.format_exc(),
                recovery="Review diagnostics, correct setup or start Docker Desktop, then retry.",
            )
            try:
                self.run(self.command() + ["logs", "--no-color", "--tail", "100"], timeout=30)
            except Exception as exc:
                self.event("error", "diagnostics", str(exc))
        finally:
            self.operation_started_at = None
            self.lock.release()

    def _warm_background(self):
        services = list(BACKGROUND_SERVICES)
        command = self.command()
        up = command + [
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "300",
            "--no-build",
            *services,
        ]
        try:
            self.background_phase = "starting"
            try:
                self.run(up, timeout=420)
            except RuntimeError as exc:
                if not MISSING_IMAGE.search(str(exc)):
                    raise
                self.background_phase = "building"
                self.event(
                    "info",
                    "launcher",
                    "Some production-engine images are missing; building them in the background.",
                )
                self.run(command + ["build", *services], timeout=1800)
                if self.phase in ("stopping", "stopped"):
                    return
                self.background_phase = "starting"
                self.run(up, timeout=420)
            if self.phase in ("stopping", "stopped"):
                self.run(command + ["stop", "--timeout", "30"], timeout=120)
                return
            self.background_phase = "ready"
            self.background_error = ""
            self.event("info", "launcher", "All production engines are ready.")
            try:
                self.check()
            except Exception as exc:
                self.event("warning", "health", str(exc))
        except Exception as exc:
            if self.phase in ("stopping", "stopped"):
                return
            self.background_phase = "degraded"
            self.background_error = self.redact(str(exc))
            self.event(
                "error",
                "launcher",
                f"Workspace is usable, but background engine warmup failed: {exc}",
                recovery="Use diagnostics to identify the affected engine; the workspace remains available.",
            )

    def start_logs(self):
        if self.follow and self.follow.poll() is None:
            return
        self.follow = subprocess.Popen(
            self.command() + ["logs", "-f", "--no-color", "--timestamps", "--tail", "100"],
            cwd=self.root,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process = self.follow

        def consume():
            for line in process.stdout:
                self.event(
                    "error"
                    if re.search(r"error|exception|traceback|fatal", line, re.I)
                    else "info",
                    "service",
                    line.rstrip(),
                )
            self.event(
                "warning", "diagnostics", "Service log stream ended; monitoring will reconnect."
            )

        threading.Thread(target=consume, daemon=True).start()

    def reconcile_existing(self):
        """Adopt an existing Katcha Compose stack without rebuilding or relaunching it."""
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self.phase = "reconnecting"
            self.stage = "checking existing runtime"
            output = self.run(
                self.command() + ["ps", "--all", "--format", "json"],
                timeout=30,
                capture=True,
            )
            rows = (
                json.loads(output)
                if output.strip().startswith("[")
                else [json.loads(line) for line in output.splitlines() if line.strip()]
            )
            self.services = [
                {key: row.get(key) for key in ("Service", "State", "Health", "ExitCode")}
                for row in rows
            ]
            if not rows:
                self.phase = "idle"
                self.stage = "idle"
                return True

            long_running = [
                row
                for row in self.services
                if row["Service"] not in ("migrate", "minio-init")
            ]
            if long_running and not any(row["State"] == "running" for row in long_running):
                self.phase = "stopped"
                self.stage = "stopped"
                self.event(
                    "info",
                    "launcher",
                    "Existing Katcha containers are stopped; leaving them stopped.",
                )
                return True

            self.phase = "degraded"
            self.stage = "reattaching"
            self.start_logs()
            self.check()
            self.stage = "ready" if self.phase == "ready" else "degraded"
            self.event(
                "info" if self.phase == "ready" else "warning",
                "launcher",
                "Reattached to the existing Katcha runtime.",
            )
            return True
        except (FileNotFoundError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            self.phase = "degraded"
            self.stage = "waiting for runtime"
            self.event(
                "warning",
                "launcher",
                f"Could not inspect the existing Katcha runtime: {exc}",
                recovery="The launcher will keep checking Docker and the existing services.",
            )
            return False
        finally:
            self.lock.release()

    def check(self):
        output = self.run(self.command() + ["ps", "--all", "--format", "json"], capture=True)
        rows = (
            json.loads(output)
            if output.strip().startswith("[")
            else [json.loads(line) for line in output.splitlines() if line.strip()]
        )
        self.services = [
            {key: row.get(key) for key in ("Service", "State", "Health", "ExitCode")}
            for row in rows
        ]
        by_service = {row["Service"]: row for row in self.services}
        one_shot_bad = [
            row
            for row in self.services
            if row["Service"] in ONE_SHOT_SERVICES and row["ExitCode"] not in (0, "0", None)
        ]
        core_bad = [
            by_service[name]
            for name in CORE_REQUIRED
            if name in by_service
            and (
                by_service[name]["State"] != "running"
                or by_service[name]["Health"] not in (None, "", "healthy")
            )
        ]
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
        try:
            connection.request("GET", "/v1/health/ready")
            ready = connection.getresponse().status == 200
        finally:
            connection.close()
        present = set(by_service)
        phase = (
            "ready"
            if ready and CORE_REQUIRED <= present and not core_bad and not one_shot_bad
            else "degraded"
        )
        if BACKGROUND_REQUIRED <= present:
            background_bad = [
                by_service[name]
                for name in BACKGROUND_REQUIRED
                if (
                    by_service[name]["State"] != "running"
                    or by_service[name]["Health"] not in (None, "", "healthy")
                )
            ]
            if not background_bad and self.background_phase not in ("building", "starting"):
                self.background_phase = "ready"
        if phase != self.phase:
            self.event(
                "info" if phase == "ready" else "error", "health", phase, services=self.services
            )
        self.phase = phase

    def monitor(self):
        while True:
            time.sleep(10)
            if self.phase in ("ready", "degraded") and self.lock.acquire(blocking=False):
                try:
                    self.check()
                    self.start_logs()
                except Exception as exc:
                    self.phase = "degraded"
                    self.event("error", "health", str(exc))
                finally:
                    self.lock.release()

    def snapshot(self):
        with self.events_lock:
            events = list(self.events)
        elapsed = (
            max(0, int(time.monotonic() - self.operation_started_at))
            if self.operation_started_at is not None
            else None
        )
        return dict(
            phase=self.phase,
            stage=self.stage,
            operation_elapsed_seconds=elapsed,
            background_phase=self.background_phase,
            background_error=self.background_error,
            session=self.session,
            services=self.services,
            events=events,
            settings={
                k: ("configured" if self.values.get(k) else "")
                if SENSITIVE.search(k)
                else self.values.get(k, "")
                for k in sorted(FIELDS)
            },
        )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Request URLs can contain OAuth credentials.

    def send(self, status, data, mime="application/json"):
        payload = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self):
        host = self.headers.get("Host")
        hosts = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return host in hosts and (not origin or origin in {f"http://{h}" for h in hosts})

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {"error": "Local requests only"})
        if self.path == "/":
            return self.send(200, (ROOT / "launcher" / "index.html").read_bytes(), "text/html")
        if self.path == "/runtime/status":
            return self.send(200, self.server.runtime.snapshot())
        if self.path == "/runtime/diagnostics":
            runtime = self.server.runtime
            # Flush and export the retained, already-redacted journal including earlier sessions.
            chunks = []
            for suffix in [".5", ".4", ".3", ".2", ".1", ""]:
                path = runtime.directory / ("events.jsonl" + suffix)
                if path.exists():
                    chunks.append(path.read_bytes())
            return self.send(200, b"".join(chunks), "application/x-ndjson")
        return self.proxy()

    def do_POST(self):
        if not self.allowed() or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.send(403, {"error": "Local requests only"})
        if not self.path.startswith("/runtime/"):
            return self.proxy()
        if self.headers.get("Content-Type") != "application/json":
            return self.send(415, {"error": "JSON required"})
        runtime = self.server.runtime
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or "{}")
            if self.path == "/runtime/client-error":
                if not isinstance(body, dict) or set(body) - {"message", "path"}:
                    raise ValueError("Invalid client diagnostic")
                runtime.event(
                    "error",
                    "browser",
                    str(body.get("message", ""))[:4000],
                    path=str(body.get("path", "")).split("?")[0][:200],
                )
                return self.send(200, {"ok": True})
            if self.path == "/runtime/settings":
                if not runtime.lock.acquire(blocking=False):
                    return self.send(409, {"error": "Wait for the active operation"})
                try:
                    if not isinstance(body, dict) or set(body) - FIELDS:
                        raise ValueError("Unknown setting")
                    if any(
                        not isinstance(v, str) or any(c in v for c in "\r\n'\x00")
                        for v in body.values()
                    ):
                        raise ValueError("Settings must be single-line text without single quotes")
                    for key, choices in [
                        ("KATCHA_RENDER_BACKEND", ("local", "lambda")),
                        ("KATCHA_AI_EXECUTION_MODE", ("fixture", "live", "auto")),
                    ]:
                        if key in body and body[key] not in choices:
                            raise ValueError(f"Invalid {key}")
                    runtime.save(body)
                    runtime.event("info", "setup", "Settings saved; Start applies changes")
                finally:
                    runtime.lock.release()
                return self.send(200, {"ok": True})
            action = self.path.removeprefix("/runtime/")
            if action not in ("start", "stop"):
                return self.send(404, {"error": "Unknown action"})
            return self.send(202 if runtime.operate(action) else 409, {"action": action})
        except (ValueError, OSError) as exc:
            runtime.event("error", "setup", str(exc))
            return self.send(400, {"error": runtime.redact(exc)})

    def proxy(self):
        if not self.path.startswith(("/v1/", "/editing", "/explorer", "/ingestion")):
            return self.send(404, {"error": "Not found"})
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=120)
        headers_sent = False
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 10_000_000:
                return self.send(413, {"error": "Request too large"})
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() in ("content-type", "accept", "range")
            }
            headers["Authorization"] = (
                "Bearer " + self.server.runtime.values["KATCHA_CONTROL_API_TOKEN"]
            )
            connection.request(self.command, self.path, self.rfile.read(length), headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in (
                    "content-type",
                    "content-length",
                    "location",
                    "content-range",
                    "accept-ranges",
                    "content-disposition",
                ):
                    self.send_header(key, value)
            self.end_headers()
            headers_sent = True
            while chunk := response.read(65536):
                self.wfile.write(chunk)
            if response.status >= 500:
                self.server.runtime.event(
                    "error", "api", f"HTTP {response.status}", path=self.path.split("?")[0]
                )
        except (OSError, ValueError) as exc:
            self.server.runtime.event("error", "gateway", str(exc))
            if not headers_sent:
                self.send(502, {"error": "Katcha is unavailable. Open the launch console."})
            else:
                self.close_connection = True
        finally:
            connection.close()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def main():
    parser = argparse.ArgumentParser(description="Launch the Katcha application")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--auto-start",
        action="store_true",
        help="Start services immediately instead of waiting for the Start Katcha button.",
    )
    parser.add_argument("--no-start", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    url = "http://localhost:8765"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    except OSError:
        print("Port 8765 is in use. If Katcha is already running, open " + url)
        return 1
    runtime = Runtime()
    server.runtime = runtime
    runtime.event("info", "launcher", "Launch console listening on " + url)
    threading.Thread(target=runtime.monitor, daemon=True).start()
    if args.auto_start and not args.no_start:
        runtime.operate("start")
    elif not args.no_start:
        threading.Thread(target=runtime.reconcile_existing, daemon=True).start()
    try:
        if not args.no_browser:
            if (
                Path("/proc/sys/kernel/osrelease").exists()
                and "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
            ):
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                webbrowser.open(url)
    except OSError as exc:
        runtime.event("warning", "browser", str(exc), recovery="Open " + url)
    print("Katcha: " + url + " — close with Ctrl+C; services remain running.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if runtime.follow:
            runtime.follow.terminate()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
