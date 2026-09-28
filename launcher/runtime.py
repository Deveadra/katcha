"""Local Katcha supervisor. Python standard library only; no virtualenv required."""

from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import datetime as dt
import hashlib
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
REQUIRED_SERVICES = {
    "postgres",
    "temporal",
    "minio",
    "api",
    "worker",
    "analysis-worker",
    "renderer",
    "production-worker",
    "longform-worker",
    "publishing-worker",
    "discovery-worker",
    "trends-worker",
    "intelligence-worker",
}

ONE_SHOT_SERVICES = {"migrate", "minio-init"}
VISIBLE_SERVICES = REQUIRED_SERVICES | ONE_SHOT_SERVICES | {"temporal-ui"}
SHARED_CORE_SERVICES = {
    "api",
    "migrate",
    "minio-init",
    "worker",
    "publishing-worker",
    "discovery-worker",
    "trends-worker",
    "intelligence-worker",
}
BUILD_SERVICE_FOR = {
    **{service: "api" for service in SHARED_CORE_SERVICES},
    "minio": "minio",
    "analysis-worker": "analysis-worker",
    "renderer": "renderer",
    "production-worker": "production-worker",
    "longform-worker": "longform-worker",
}
PULLABLE_SERVICES = {"postgres", "temporal", "temporal-ui"}

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
        self.desired_running = (self.directory / "desired-state").exists()
        self.workspace_ready = False
        self.health_check_at = 0.0
        self.retry_at = 0.0
        self.retry_delay = 10
        self.phase = "idle"
        self.stage = "idle"
        self.operation_started_at = None
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
            workspace_ready=self.workspace_ready,
            desired_running=self.desired_running,
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
                if capture:
                    output.append(line)
                else:
                    self.event("info", "compose", line.rstrip())
            code = process.wait(timeout=max(1, deadline - time.monotonic()))
            if code:
                if capture:
                    self.event("error", "compose", "".join(output))
                raise RuntimeError(f"Command failed (exit {code}): {args[-1]}")
            return "".join(output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def build_fingerprint(self):
        """Hash actual image inputs, excluding caches and credentials."""
        digest = hashlib.sha256()
        paths = list(self.root.glob("Dockerfile*")) + list(self.root.glob("docker-compose*.yml"))
        paths += [self.root / name for name in ("pyproject.toml", "README.md", "alembic.ini")]
        for name in ("src", "migrations", "renderer"):
            paths.extend((self.root / name).rglob("*"))
        for path in sorted(set(paths)):
            if not path.is_file() or any(
                part in {"node_modules", "__pycache__", ".cache"} for part in path.parts
            ) or path.suffix == ".pyc":
                continue
            digest.update(str(path.relative_to(self.root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def core_build_fingerprint(self):
        """Hash only inputs needed by the interactive API/runtime image."""
        digest = hashlib.sha256()
        paths = [
            self.root / "Dockerfile",
            self.root / "pyproject.toml",
            self.root / "README.md",
            self.root / "alembic.ini",
            self.root / "docker-compose.yml",
            self.root / "docker-compose.app.yml",
        ]
        for name in ("src", "migrations"):
            paths.extend((self.root / name).rglob("*"))
        for path in sorted(set(paths)):
            if not path.is_file() or any(
                part in {"node_modules", "__pycache__", ".cache"} for part in path.parts
            ) or path.suffix == ".pyc":
                continue
            digest.update(str(path.relative_to(self.root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def prepare_core_image(self):
        fingerprint = self.core_build_fingerprint()
        stamp = self.directory / "core-build-fingerprint"
        cached = stamp.exists() and stamp.read_text() == fingerprint
        if cached:
            try:
                self.run(["docker", "image", "inspect", "katcha-python-core:local"], capture=True)
            except RuntimeError:
                cached = False
        if cached:
            self.event("info", "launcher", "Reusing unchanged interactive core image.")
            return
        self.stage = "preparing interactive core"
        self.event("info", "launcher", "Preparing the interactive Katcha core image.")
        self.run(self.command() + ["build", "api"], timeout=1200)
        temporary = stamp.with_suffix(".pending")
        temporary.write_text(fingerprint)
        temporary.replace(stamp)

    def prepare_images(self):
        fingerprint = self.build_fingerprint()
        stamp = self.directory / "build-fingerprint"
        images = self.run(self.command() + ["config", "--images"], capture=True).split()
        cached = stamp.exists() and stamp.read_text() == fingerprint and bool(images)
        if cached:
            try:
                self.run(["docker", "image", "inspect", *sorted(set(images))], capture=True)
            except RuntimeError:
                cached = False
        if cached:
            self.event("info", "launcher", "Reusing unchanged application images.")
            return
        self.stage = "building images"
        self.event("info", "launcher", "Preparing new or changed application images.")
        self.run(self.command() + ["build"], timeout=1800)
        temporary = stamp.with_suffix(".pending")
        temporary.write_text(fingerprint)
        temporary.replace(stamp)

    def service_actions(self, service):
        actions = ["restart", "recreate"]
        if service in BUILD_SERVICE_FOR:
            actions.append("rebuild")
        if service in PULLABLE_SERVICES:
            actions.append("redownload")
        return actions

    def service_health(self, row):
        service = row.get("Service")
        state = str(row.get("State") or "").lower()
        health = str(row.get("Health") or "").lower()
        exit_code = row.get("ExitCode")
        if row.get("Missing"):
            return "red"
        if service in ONE_SHOT_SERVICES:
            return "green" if exit_code == 0 else "red"
        if state != "running":
            return "red"
        if health in {"unhealthy"}:
            return "red"
        if health in {"starting", "unknown"}:
            return "yellow"
        return "green"

    def visible_services(self):
        rows = {row.get("Service"): dict(row) for row in self.services if row.get("Service")}
        for service in sorted(VISIBLE_SERVICES - set(rows)):
            rows[service] = {
                "Service": service,
                "State": "unavailable",
                "Health": "",
                "ExitCode": None,
                "Missing": True,
            }
        result = []
        for service in sorted(rows):
            row = rows[service]
            row["StatusColor"] = self.service_health(row)
            row["Actions"] = self.service_actions(service)
            result.append(row)
        return result

    def repair_service(self, service, action):
        if service not in VISIBLE_SERVICES:
            raise ValueError("Unknown service")
        if action not in self.service_actions(service):
            raise ValueError("Unsupported repair action for service")
        if not self.lock.acquire(blocking=False):
            return False

        def repair():
            try:
                self.event("info", "repair", f"{action} requested for {service}")
                if action == "restart":
                    self.run(self.command() + ["restart", service], timeout=180)
                elif action == "recreate":
                    self.run(
                        self.command()
                        + ["up", "-d", "--no-deps", "--no-build", "--force-recreate", service],
                        timeout=240,
                    )
                elif action == "rebuild":
                    build_service = BUILD_SERVICE_FOR[service]
                    self.run(self.command() + ["build", build_service], timeout=1800)
                    self.run(
                        self.command()
                        + ["up", "-d", "--no-deps", "--no-build", "--force-recreate", service],
                        timeout=240,
                    )
                elif action == "redownload":
                    self.run(self.command() + ["pull", service], timeout=900)
                    self.run(
                        self.command()
                        + ["up", "-d", "--no-deps", "--force-recreate", service],
                        timeout=240,
                    )
                self.check()
                self.event("info", "repair", f"{service} repair completed", action=action)
            except Exception:
                self.event(
                    "error",
                    "repair",
                    traceback.format_exc(),
                    service=service,
                    action=action,
                    recovery=(
                        "Try the next stronger per-service repair action "
                        "or inspect diagnostics."
                    ),
                )
                with contextlib.suppress(Exception):
                    self.check()
            finally:
                self.lock.release()

        threading.Thread(target=repair, daemon=True).start()
        return True

    def operate(self, action):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            marker = self.directory / "desired-state"
            if action == "start":
                marker.write_text("running\n")
            else:
                marker.unlink(missing_ok=True)
            self.desired_running = action == "start"
        except OSError:
            self.lock.release()
            raise
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
                self.run(self.command() + ["stop", "--timeout", "30"], timeout=120)
                self.phase = "stopped"
                self.stage = "stopped"
                return
            self.stage = "validating configuration"
            self.run(self.command() + ["config", "--quiet"])

            # Boot the interactive shell first. The API is intentionally decoupled from
            # Temporal/MinIO in the application overlay so heavy production capabilities
            # cannot block the operator from entering Katcha.
            self.prepare_core_image()
            self.stage = "starting interactive core"
            self.event("info", "launcher", "Starting the interactive Katcha core.")
            self.run(
                self.command()
                + [
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "api",
                ],
                timeout=180,
            )
            if not self.probe_workspace():
                raise RuntimeError("Interactive API started but did not become reachable.")
            self.start_logs()
            self.stage = "warming background capabilities"
            self.event(
                "info",
                "launcher",
                "Workspace is ready; background production capabilities are warming.",
            )

            # The expensive renderer and worker images prepare only after the UI is usable.
            self.prepare_images()
            self.stage = "starting background capabilities"
            self.event("info", "launcher", "Starting background production capabilities.")
            self.run(
                self.command() + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "300"],
                timeout=420,
            )
            self.stage = "checking full readiness"
            self.check()
            if self.phase != "ready":
                raise RuntimeError(
                    "Workspace is available but background capability readiness failed."
                )
            self.stage = "ready"
            self.retry_delay = 10
            self.event("info", "launcher", "Katcha is fully ready")
        except Exception:
            self.phase = "degraded" if self.workspace_ready else "failed"
            self.stage = (
                "background capability warm-up failed" if self.workspace_ready else "failed"
            )
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
            self.retry_at = time.monotonic() + self.retry_delay
            self.retry_delay = min(300, self.retry_delay * 2)
            self.operation_started_at = None
            self.lock.release()

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
        bad = [
            r
            for r in self.services
            if (r["Service"] in ("migrate", "minio-init") and r["ExitCode"] != 0)
            or (
                r["Service"] not in ("migrate", "minio-init")
                and (r["State"] != "running" or r["Health"] not in (None, "", "healthy"))
            )
        ]
        ready = self.probe_workspace()
        present = {row["Service"] for row in self.services}
        phase = "ready" if ready and present >= REQUIRED_SERVICES and not bad else "degraded"
        if phase != self.phase:
            self.event(
                "info" if phase == "ready" else "error", "health", phase, services=self.services
            )
        self.phase = phase

    def probe_workspace(self):
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=2)
        try:
            connection.request("GET", "/v1/health/live")
            self.workspace_ready = connection.getresponse().status == 200
        except (OSError, http.client.HTTPException):
            self.workspace_ready = False
        finally:
            connection.close()
        return self.workspace_ready

    def monitor_once(self):
        # Probe independently of the startup lock: workers must not gate GUI access.
        self.probe_workspace()
        if (self.phase in ("ready", "degraded")
                and time.monotonic() >= self.health_check_at
                and self.lock.acquire(blocking=False)):
            self.health_check_at = time.monotonic() + 10
            try:
                self.check()
                self.start_logs()
            except Exception as exc:
                self.phase = "degraded"
                self.event("error", "health", str(exc))
            finally:
                self.lock.release()
        # Reconcile missing/exited containers without falling back to a full-stack rebuild.
        if not self.desired_running or time.monotonic() < self.retry_at:
            return
        if self.phase in ("failed", "idle", "stopped") and not self.workspace_ready:
            self.operate("start")
            return
        if self.phase != "degraded":
            return
        present = {row["Service"] for row in self.services}
        missing = sorted(REQUIRED_SERVICES - present)
        if missing:
            self.repair_service(missing[0], "recreate")
            return
        exited = next(
            (
                row["Service"]
                for row in self.services
                if row["Service"] not in ONE_SHOT_SERVICES and row["State"] != "running"
            ),
            None,
        )
        if exited:
            self.repair_service(exited, "restart")

    def monitor(self):
        while True:
            time.sleep(2)
            try:
                self.monitor_once()
            except Exception as exc:
                self.event("error", "supervisor", str(exc))

    def snapshot(self):
        with self.events_lock:
            events = list(self.events)
        elapsed = (
            max(0, int(time.monotonic() - self.operation_started_at))
            if self.operation_started_at is not None
            else None
        )
        return dict(
            workspace_ready=self.workspace_ready,
            desired_running=self.desired_running,
            phase=self.phase,
            stage=self.stage,
            operation_elapsed_seconds=elapsed,
            session=self.session,
            services=self.visible_services(),
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
            if self.path == "/runtime/service":
                if not isinstance(body, dict) or set(body) != {"service", "action"}:
                    raise ValueError("service and action are required")
                service = str(body["service"])
                action = str(body["action"])
                if not runtime.repair_service(service, action):
                    return self.send(409, {"error": "Wait for the active operation"})
                return self.send(202, {"ok": True, "service": service, "action": action})
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
    if (args.auto_start or runtime.desired_running) and not args.no_start:
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
