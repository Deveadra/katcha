"""Local Katcha supervisor. Python standard library only; no virtualenv required."""

from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import http.client
import json
import logging
import logging.handlers
import mimetypes
import os
import queue
import re
import secrets
import signal
import subprocess
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_IMAGE = "katcha-control:local"
PREBUILT_REGISTRY = "ghcr.io/deveadra"
EARLY_AUTOMATION_SERVICES = (
    "temporal",
    "intelligence-worker",
)
BACKGROUND_BUILD_SERVICES = (
    "minio",
    "worker",
    "analysis-worker",
    "renderer",
    "production-worker",
)
BACKGROUND_DOCKERFILES = (
    "Dockerfile",
    "Dockerfile.analysis",
    "Dockerfile.minio",
    "Dockerfile.production",
    "Dockerfile.renderer",
)
REQUIRED_SERVICES = {
    "postgres",
    "temporal",
    "minio",
    "api",
    "worker",
    "analysis-worker",
    "renderer",
    "production-worker",
    "telegram-worker",
    "intelligence-worker",
}

FIELDS = {
    "KATCHA_OPENAI_API_KEY",
    "KATCHA_GEMINI_API_KEY",
    "KATCHA_AI_EXECUTION_MODE",
    "KATCHA_CONVERSATION_PROVIDER",
    "KATCHA_AGENT_PROVIDER",
    "KATCHA_ALLOW_PAID_OPENAI_FALLBACK",
    "KATCHA_YOUTUBE_CLIENT_ID",
    "KATCHA_YOUTUBE_CLIENT_SECRET",
    "KATCHA_YOUTUBE_DATA_API_KEY",
    "KATCHA_TELEGRAM_ENABLED",
    "KATCHA_TELEGRAM_BOT_TOKEN",
    "KATCHA_TELEGRAM_CHAT_ID",
    "KATCHA_TELEGRAM_ALLOWED_USER_ID",
    "KATCHA_TELEGRAM_PAIRING_CODE",
    "KATCHA_TELEGRAM_REVIEW_STORAGE_ENDPOINT_URL",
    "KATCHA_RENDER_BACKEND",
    "KATCHA_AWS_PROFILE",
    "KATCHA_AWS_EXPECTED_ACCOUNT_ID",
    "KATCHA_REMOTION_LAMBDA_REGION",
    "KATCHA_REMOTION_LAMBDA_FUNCTION_NAME",
    "KATCHA_REMOTION_LAMBDA_SERVE_URL",
    "KATCHA_REMOTION_STAGING_BUCKET",
}
SENSITIVE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
HANDOFF_MAX_BYTES = 10 * 1024 * 1024
HANDOFF_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.json$")
WORKSPACE_MIME_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def workspace_asset_mime(path: Path) -> str:
    """Return browser-safe MIME types without depending on host MIME databases."""
    return (
        WORKSPACE_MIME_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
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
        self.health_lock = threading.Lock()
        self.desired_running = (self.directory / "desired-state").exists()
        self.workspace_ready = False
        self.health_check_at = 0.0
        self.workspace_check_at = 0.0
        self.retry_at = 0.0
        self.retry_delay = 10
        self.phase = "idle"
        self.stage = "idle"
        self.operation_started_at = None
        self.known_secrets = set()
        self.services = []
        self.follow = None
        self.follow_thread = None
        self.shutdown_event = threading.Event()
        self.monitor_thread = None
        self.logger = logging.getLogger(self.session)
        self.logger.setLevel(logging.INFO)
        handler = logging.handlers.RotatingFileHandler(
            self.directory / "events.jsonl", maxBytes=5_000_000, backupCount=5
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)
        self.bootstrap()

    def bootstrap(self):
        handoff = self.root / "handoff"
        handoff.mkdir(parents=True, exist_ok=True)
        handoff.chmod(0o770)
        for name in ("incoming", "processed", "failed", "receipts"):
            path = handoff / name
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o770)
        if not self.env_path.exists():
            with open(self.env_path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                stream.write((self.root / ".env.example").read_text())
        values = read_env(self.env_path)
        additions = {}
        if not values.get("KATCHA_CHATGPT_HOST_ID"):
            additions["KATCHA_CHATGPT_HOST_ID"] = f"urn:uuid:{uuid.uuid4()}"
        if not values.get("KATCHA_CREDENTIAL_ENCRYPTION_KEY"):
            additions["KATCHA_CREDENTIAL_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
                secrets.token_bytes(32)
            ).decode()
        if not values.get("KATCHA_CONTROL_API_TOKEN"):
            additions["KATCHA_CONTROL_API_TOKEN"] = secrets.token_urlsafe(32)
        if not values.get("KATCHA_TELEGRAM_PAIRING_CODE"):
            additions["KATCHA_TELEGRAM_PAIRING_CODE"] = secrets.token_urlsafe(9)
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
        if self.shutdown_event.is_set():
            return
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
            try:
                for line in process.stdout:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        reader = threading.Thread(
            target=read,
            daemon=True,
            name="katcha-command-reader",
        )
        reader.start()
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
            reader.join(timeout=3)
            if reader.is_alive():
                with contextlib.suppress(OSError, ValueError):
                    process.stdout.close()
                reader.join(timeout=1)
            if not reader.is_alive():
                with contextlib.suppress(OSError, ValueError):
                    process.stdout.close()

    def release_revision(self):
        """Return the publishable main-branch revision, or None for local/dev trees."""
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.root,
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            if branch != "main":
                return None
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.root,
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            if dirty:
                return None
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        return revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None

    def prebuilt_ref(self, local_image, revision):
        repository = local_image.split(":", 1)[0]
        return f"{PREBUILT_REGISTRY}/{repository}:{revision}"

    def install_prebuilt_images(self, local_images, pull_timeout=600):
        """Install every available exact-revision image without blocking on missing ones."""
        revision = self.release_revision()
        if not revision:
            return set()
        pairs = [
            (local_image, self.prebuilt_ref(local_image, revision))
            for local_image in local_images
        ]

        def install(pair):
            local_image, remote = pair
            try:
                # Probe first so a private/unpublished registry entry fails quickly instead
                # of consuming the full image-pull timeout on the interactive path.
                self.run(
                    ["docker", "manifest", "inspect", remote],
                    timeout=10,
                    capture=True,
                )
                self.run(
                    ["docker", "pull", remote],
                    timeout=pull_timeout,
                    capture=True,
                )
                self.run(
                    ["docker", "tag", remote, local_image],
                    timeout=30,
                    capture=True,
                )
                return local_image, None
            except (RuntimeError, TimeoutError, OSError) as exc:
                return local_image, exc

        self.stage = "pulling prebuilt images"
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(4, len(pairs))
        ) as pool:
            results = list(pool.map(install, pairs))

        installed = {image for image, error in results if error is None}
        missing = [(image, error) for image, error in results if error is not None]
        if installed:
            self.event(
                "info",
                "launcher",
                "Installed prebuilt runtime images.",
                revision=revision[:12],
                image_count=len(installed),
            )
        if missing:
            self.event(
                "warning",
                "launcher",
                "Some prebuilt runtime images are unavailable; building only the missing images.",
                revision=revision[:12],
                missing_images=[image for image, _ in missing],
                reason="; ".join(str(error) for _, error in missing),
            )
        return installed

    def build_fingerprint(self, workspace=False):
        """Hash image inputs, with a smaller fingerprint for the interactive control plane."""
        digest = hashlib.sha256()
        dockerfiles = (
            [self.root / "Dockerfile.control"]
            if workspace
            else [self.root / name for name in BACKGROUND_DOCKERFILES]
        )
        compose_files = [
            self.root / "docker-compose.yml",
            self.root / "docker-compose.discovery.yml",
            self.root / "docker-compose.trends.yml",
            self.root / "docker-compose.intelligence.yml",
            self.root / "docker-compose.app.yml",
        ]
        if self.values.get("KATCHA_RENDER_BACKEND") == "lambda":
            compose_files.append(self.root / "docker-compose.aws-render.yml")
            if self.values.get("KATCHA_AWS_SIGNING_HELPER_PATH"):
                compose_files.append(self.root / "docker-compose.aws-roles-anywhere.yml")
        paths = dockerfiles + compose_files
        paths += [self.root / name for name in ("pyproject.toml", "README.md", "alembic.ini")]
        source_trees = ("src", "migrations") if workspace else ("src", "migrations", "renderer")
        for name in source_trees:
            paths.extend((self.root / name).rglob("*"))
        for path in sorted(set(paths)):
            if not path.is_file() or any(
                part in {"node_modules", "__pycache__", ".cache"} for part in path.parts
            ) or path.suffix == ".pyc":
                continue
            digest.update(str(path.relative_to(self.root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def prepare_workspace_image(self):
        fingerprint = self.build_fingerprint(workspace=True)
        stamp = self.directory / "workspace-build-fingerprint"
        cached = stamp.exists() and stamp.read_text() == fingerprint
        if cached:
            try:
                self.run(["docker", "image", "inspect", WORKSPACE_IMAGE], capture=True)
            except RuntimeError:
                cached = False
        if cached:
            self.event("info", "launcher", "Reusing unchanged workspace image.")
            return
        installed = self.install_prebuilt_images(
            [WORKSPACE_IMAGE],
            pull_timeout=180,
        )
        if WORKSPACE_IMAGE in installed:
            temporary = stamp.with_suffix(".pending")
            temporary.write_text(fingerprint)
            temporary.replace(stamp)
            self.event("info", "launcher", "Using prebuilt workspace image.")
            return
        self.stage = "building workspace image"
        self.event(
            "info",
            "launcher",
            "Preparing the lightweight workspace control plane locally.",
        )
        self.run(self.command() + ["build", "api"], timeout=900)
        temporary = stamp.with_suffix(".pending")
        temporary.write_text(fingerprint)
        temporary.replace(stamp)

    def image_build_services(self):
        minio_version = self.values.get(
            "KATCHA_MINIO_VERSION", "RELEASE.2025-10-15T17-29-55Z"
        )
        return {
            "katcha-ingest:local": "worker",
            "katcha-analysis:local": "analysis-worker",
            "katcha-renderer:local": "renderer",
            "katcha-production:local": "production-worker",
            f"katcha-minio:{minio_version}": "minio",
        }

    def built_images(self):
        return list(self.image_build_services())

    def prepare_images(self):
        fingerprint = self.build_fingerprint()
        stamp = self.directory / "build-fingerprint"
        images = self.built_images()
        cached = stamp.exists() and stamp.read_text() == fingerprint
        if cached:
            try:
                self.run(["docker", "image", "inspect", *sorted(set(images))], capture=True)
            except RuntimeError:
                cached = False
        if cached:
            self.event("info", "launcher", "Reusing unchanged application images.")
            return
        installed = self.install_prebuilt_images(images)
        build_services = self.image_build_services()
        missing_services = [
            build_services[image]
            for image in images
            if image not in installed
        ]
        if missing_services:
            self.stage = "building images"
            self.event(
                "info",
                "launcher",
                "Preparing only missing or changed application images locally.",
                services=missing_services,
            )
            self.run(
                self.command() + ["build", *missing_services],
                timeout=1800,
            )
        else:
            self.event("info", "launcher", "Using prebuilt automation images.")
        temporary = stamp.with_suffix(".pending")
        temporary.write_text(fingerprint)
        temporary.replace(stamp)

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
        final_phase = None
        final_stage = None
        final_message = None
        final_seconds = None
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
                final_phase = "stopped"
                final_stage = "stopped"
                return
            self.stage = "validating configuration"
            self.run(self.command() + ["config", "--quiet"])

            # Keep the interactive launch serialized at the Docker boundary. Running
            # Compose startup and image preparation concurrently is fragile on WSL/Docker
            # Desktop and can make the launcher itself unreachable under resource pressure.
            self.prepare_workspace_image()

            self.stage = "starting workspace"
            self.event("info", "launcher", "Starting the interactive workspace.")
            self.run(
                self.command()
                + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "api"],
                timeout=180,
            )
            self.start_logs()
            if not self.probe_workspace():
                raise RuntimeError("Workspace services started but the control plane is not ready.")
            self.event(
                "info",
                "launcher",
                "Workspace ready; warming automation in the background.",
                startup_seconds=round(time.monotonic() - self.operation_started_at, 2),
            )

            # Discovery/trend intelligence does not depend on media storage or rendering.
            # Bring it online before heavyweight media images (especially MinIO) are built.
            self.stage = "starting discovery intelligence"
            self.event(
                "info",
                "launcher",
                "Starting always-on discovery, trends, and intelligence services.",
            )
            self.run(
                self.command()
                + [
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    *EARLY_AUTOMATION_SERVICES,
                ],
                timeout=240,
            )
            self.event(
                "info",
                "launcher",
                "Discovery intelligence is online; warming the media factory.",
                startup_seconds=round(time.monotonic() - self.operation_started_at, 2),
            )

            self.stage = "warming automation images"
            self.prepare_images()
            self.stage = "warming automation services"
            self.event(
                "info",
                "launcher",
                "Starting production automation, storage, orchestration, and rendering.",
            )
            self.run(
                self.command() + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "300"],
                timeout=420,
            )
            self.stage = "checking full readiness"
            if self.check(publish=False) != "ready":
                raise RuntimeError(
                    "Workspace is available but background services need attention."
                )
            # Do not publish READY until the lifecycle lock has been released. Otherwise
            # a user can see READY and immediately receive 409 from Start/Stop.
            self.stage = "finalizing startup"
            self.retry_delay = 10
            final_phase = "ready"
            final_stage = "ready"
            final_message = "Katcha automation is fully ready."
            final_seconds = round(time.monotonic() - self.operation_started_at, 2)
        except Exception:
            self.probe_workspace()
            self.phase = "degraded" if self.workspace_ready else "failed"
            self.stage = "automation warm-up failed" if self.workspace_ready else "failed"
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
            if final_phase is not None:
                self.phase = final_phase
                self.stage = final_stage
                if final_message:
                    self.event(
                        "info",
                        "launcher",
                        final_message,
                        startup_seconds=final_seconds,
                    )

    def apply_ai(self):
        """Reload only services that cache AI provider configuration."""
        self.values = read_env(self.env_path)
        self.known_secrets.update(
            v for k, v in self.values.items() if v and SENSITIVE.search(k)
        )
        if not self.desired_running or self.phase in ("idle", "stopped", "failed"):
            self.event(
                "info",
                "ai",
                "AI settings saved; Start Katcha will apply them.",
            )
            return False
        self.event(
            "info",
            "ai",
            "Applying AI settings without restarting storage, Temporal, or renderer.",
        )
        self.run(
            self.command()
            + [
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                "api",
                "worker",
                "analysis-worker",
                "production-worker",
                "intelligence-worker",
            ],
            timeout=180,
        )
        if not self.probe_workspace():
            raise RuntimeError("AI settings were saved, but the Katcha API did not recover.")
        self.start_logs()
        self.event("info", "ai", "AI settings applied.")
        return True

    def apply_telegram(self):
        """Reload only the services that cache Telegram settings."""
        self.values = read_env(self.env_path)
        self.known_secrets.update(
            v for k, v in self.values.items() if v and SENSITIVE.search(k)
        )
        if not self.desired_running or self.phase in ("idle", "stopped", "failed"):
            self.event(
                "info",
                "telegram",
                "Telegram settings saved; Start Katcha will apply them.",
            )
            return False
        self.event(
            "info",
            "telegram",
            "Applying Telegram settings without restarting the media pipeline.",
        )
        self.run(
            self.command()
            + [
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "90",
                "api",
                "telegram-worker",
            ],
            timeout=150,
        )
        if not self.probe_workspace():
            raise RuntimeError("Telegram settings were saved, but the Katcha API did not recover.")
        self.start_logs()
        self.event("info", "telegram", "Telegram settings applied.")
        return True

    def start_logs(self):
        if self.shutdown_event.is_set():
            return
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
            try:
                for line in process.stdout:
                    if self.shutdown_event.is_set():
                        break
                    self.event(
                        "error"
                        if re.search(r"error|exception|traceback|fatal", line, re.I)
                        else "info",
                        "service",
                        line.rstrip(),
                    )
            except (OSError, ValueError):
                # The launcher may close the pipe while stopping. That is an
                # expected shutdown path, not an exception worth printing from
                # a daemon thread while the interpreter is finalizing.
                if not self.shutdown_event.is_set():
                    self.event(
                        "warning",
                        "diagnostics",
                        "Service log stream closed unexpectedly; monitoring will reconnect.",
                    )
            finally:
                if not self.shutdown_event.is_set() and self.follow is process:
                    self.event(
                        "warning",
                        "diagnostics",
                        "Service log stream ended; monitoring will reconnect.",
                    )

        thread = threading.Thread(
            target=consume,
            daemon=True,
            name="katcha-log-consumer",
        )
        self.follow_thread = thread
        thread.start()

    def stop_logs(self):
        """Stop and join the launcher-owned log follower without touching services."""
        process = self.follow
        thread = self.follow_thread
        self.follow = None
        self.follow_thread = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=3)
        except (OSError, ProcessLookupError):
            pass

        # Waiting for the child process normally delivers EOF to the reader.
        # Join before closing stdout so the reader cannot raise during Python
        # interpreter teardown and attempt to write a traceback to stderr.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

        stream = getattr(process, "stdout", None)
        if thread is not None and thread.is_alive() and stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()
            thread.join(timeout=1)

        if stream is not None and (thread is None or not thread.is_alive()):
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def shutdown(self):
        """Stop launcher-owned background activity; leave Katcha services running."""
        self.shutdown_event.set()
        self.stop_logs()
        thread = self.monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

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

    def check(self, publish=True):
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
        if publish and self.phase not in ("starting", "stopping", "reconnecting"):
            if phase != self.phase:
                self.event(
                    "info" if phase == "ready" else "error",
                    "health",
                    phase,
                    services=self.services,
                )
            self.phase = phase
        return phase

    def probe_workspace(self):
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=2)
        try:
            connection.request("GET", "/v1/health/workspace")
            self.workspace_ready = connection.getresponse().status == 200
        except (OSError, http.client.HTTPException):
            self.workspace_ready = False
        finally:
            connection.close()
        return self.workspace_ready

    def monitor_once(self):
        # Startup/recovery stays responsive. A healthy steady-state runtime is
        # intentionally quiet so the supervisor itself does not create idle load.
        now = time.monotonic()
        steady = self.phase in ("ready", "degraded")
        if not steady or now >= self.workspace_check_at:
            self.probe_workspace()
            self.workspace_check_at = now + (10 if self.workspace_ready else 2)
        if (
            steady
            and now >= self.health_check_at
            and self.health_lock.acquire(blocking=False)
        ):
            self.health_check_at = now + (30 if self.phase == "ready" else 10)
            try:
                self.check()
                self.start_logs()
            except Exception as exc:
                if self.phase not in ("starting", "stopping", "reconnecting"):
                    self.phase = "degraded"
                self.event("error", "health", str(exc))
            finally:
                self.health_lock.release()
        # Reconcile missing/exited containers; never repeatedly restart unhealthy ones.
        present = {row["Service"] for row in self.services}
        missing = not present >= REQUIRED_SERVICES or any(
            row["State"] != "running"
            for row in self.services
            if row["Service"] not in ("migrate", "minio-init")
        )
        if (self.desired_running and time.monotonic() >= self.retry_at
                and (self.phase in ("failed", "idle", "stopped")
                     or (self.phase == "degraded" and missing))):
            self.operate("start")

    def monitor(self):
        while not self.shutdown_event.is_set():
            delay = (
                10 if self.phase in ("ready", "idle", "stopped") and self.workspace_ready
                else 2 if self.desired_running else 10
            )
            if self.shutdown_event.wait(delay):
                return
            try:
                self.monitor_once()
            except Exception as exc:
                if self.shutdown_event.is_set():
                    return
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

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def workspace_asset(self):
        path, _, query = self.path.partition("?")
        redirects = {
            "/home": "/home/assets/home.html",
            "/editing": "/editing/assets/editing.html",
            "/explorer": "/explorer/assets/index.html",
            "/ingestion": "/editing/assets/ingestion.html",
            "/clips": "/editing/assets/clips.html",
            "/channels": "/channels/assets/channels.html",
            "/studio": "/studio/assets/studio.html",
            "/ai": "/ai/assets/ai.html",
            "/operations": "/operations/assets/operations.html",
            "/settings": "/settings/assets/settings.html",
        }
        if path in redirects:
            location = redirects[path]
            if query:
                location += "?" + query
            self.redirect(location)
            return True

        shared_roots = {
            "/system/": ROOT / "src" / "katcha" / "web" / "system",
            "/pages/": ROOT / "src" / "katcha" / "web" / "pages",
        }
        for prefix, root in shared_roots.items():
            if not path.startswith(prefix):
                continue
            name = path.removeprefix(prefix)
            if not name or Path(name).name != name:
                return False
            asset = root / name
            if not asset.is_file():
                return False
            mime = workspace_asset_mime(asset)
            self.send(200, asset.read_bytes(), mime)
            return True

        top_level_suffixes = {".css", ".js"}
        if path.count("/") == 1 and Path(path).suffix in top_level_suffixes:
            name = path.removeprefix("/")
            web_root = (ROOT / "src" / "katcha" / "web").resolve()
            asset = (web_root / name).resolve()
            try:
                asset.relative_to(web_root)
            except ValueError:
                return False
            if not asset.is_file():
                return False
            mime = workspace_asset_mime(asset)
            self.send(200, asset.read_bytes(), mime)
            return True

        prefixes = (
            "/home/assets/",
            "/editing/assets/",
            "/explorer/assets/",
            "/channels/assets/",
            "/studio/assets/",
            "/ai/assets/",
            "/operations/assets/",
            "/settings/assets/",
        )
        prefix = next((item for item in prefixes if path.startswith(item)), None)
        if prefix is None:
            return False
        name = path.removeprefix(prefix)
        if prefix == "/system/":
            name = "system/" + name
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            return False
        web_root = (ROOT / "src" / "katcha" / "web").resolve()
        asset = (web_root / relative).resolve()
        try:
            asset.relative_to(web_root)
        except ValueError:
            return False
        if not asset.is_file():
            return False
        mime = workspace_asset_mime(asset)
        self.send(200, asset.read_bytes(), mime)
        return True

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
        if self.workspace_asset():
            return
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

    def _api_json_request(self, method, path, payload=None):
        body = b""
        headers = {
            "Authorization": (
                "Bearer " + self.server.runtime.values["KATCHA_CONTROL_API_TOKEN"]
            ),
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=120)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                data = json.loads(raw or b"{}")
            except (TypeError, ValueError):
                data = {
                    "error": (
                        raw.decode("utf-8", errors="replace")[:4000]
                        or "Katcha returned an unreadable response."
                    )
                }
            return response.status, data
        finally:
            connection.close()

    def _handoff_filename(self):
        parsed = urllib.parse.urlsplit(self.path)
        values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        names = values.get("filename", [])
        filename = names[0] if len(names) == 1 else ""
        if (
            not filename
            or Path(filename).name != filename
            or not HANDOFF_FILENAME.fullmatch(filename)
        ):
            raise ValueError(
                "Handoff filename must be a simple .json name using letters, "
                "numbers, dots, dashes, or underscores."
            )
        return filename

    def _existing_handoff_path(self, filename):
        root = self.server.runtime.root / "handoff"
        for status in ("incoming", "processed", "failed"):
            path = root / status / filename
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Existing handoff path is not a regular file.")
                return path
        return None

    def _handle_handoff_upload(self):
        runtime = self.server.runtime
        filename = self._handoff_filename()
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/octet-stream":
            return self.send(415, {"error": "Handoff upload requires application/octet-stream"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Handoff upload has an invalid Content-Length.") from exc
        if not 0 < length <= HANDOFF_MAX_BYTES:
            return self.send(
                413,
                {"error": "Handoff file must be between 1 byte and 10 MiB."},
            )
        content = self.rfile.read(length)
        if len(content) != length:
            raise OSError(
                f"Handoff upload ended early ({len(content)} of {length} bytes received)."
            )

        root = runtime.root / "handoff"
        incoming = root / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        existing = self._existing_handoff_path(filename)
        if existing is not None:
            if hashlib.sha256(existing.read_bytes()).digest() != hashlib.sha256(content).digest():
                return self.send(
                    409,
                    {
                        "error": (
                            "A handoff file with this name already exists "
                            "with different content."
                        )
                    },
                )
        else:
            target = incoming / filename
            temporary = incoming / f".{filename}.{uuid.uuid4().hex}.pending"
            temporary.write_bytes(content)
            os.replace(temporary, target)

        runtime.event(
            "info",
            "handoff",
            "Handoff upload received.",
            filename=filename,
            size_bytes=length,
        )
        status, data = self._api_json_request(
            "POST",
            "/v1/intelligence-ingest/inbox/process",
            {"filenames": [filename], "limit": 1},
        )
        if status >= 400:
            runtime.event(
                "error",
                "handoff",
                "Katcha rejected the handoff processing request.",
                filename=filename,
                status_code=status,
            )
            return self.send(status, data)
        if not isinstance(data, list) or not data:
            runtime.event(
                "error",
                "handoff",
                "Katcha returned no result for the uploaded handoff.",
                filename=filename,
            )
            return self.send(
                502,
                {"error": "Katcha accepted the file but returned no processing result."},
            )
        result = data[0]
        runtime.event(
            "info" if result.get("status") == "processed" else "error",
            "handoff",
            "Handoff processing finished.",
            filename=filename,
            handoff_status=str(result.get("status") or "unknown"),
            record_count=result.get("record_count"),
        )
        return self.send(200, result)

    def _handle_handoff_process(self):
        runtime = self.server.runtime
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Handoff process request has an invalid Content-Length.") from exc
        if not 0 <= length <= 65536:
            return self.send(413, {"error": "Handoff process request is too large."})
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ValueError("Handoff process request must contain valid JSON.") from exc
        if not isinstance(body, dict) or set(body) - {"limit"}:
            raise ValueError("Handoff process request contains unsupported fields.")
        limit = body.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Handoff process limit must be between 1 and 500.")

        runtime.event("info", "handoff", "Processing pending handoff files.", limit=limit)
        status, data = self._api_json_request(
            "POST",
            "/v1/intelligence-ingest/inbox/process",
            {"limit": limit},
        )
        if status >= 400:
            runtime.event(
                "error",
                "handoff",
                "Katcha rejected the pending handoff processing request.",
                status_code=status,
            )
            return self.send(status, data)
        processed = len(data) if isinstance(data, list) else 0
        failed = (
            sum(1 for item in data if item.get("status") == "failed")
            if isinstance(data, list)
            else 0
        )
        runtime.event(
            "info" if failed == 0 else "warning",
            "handoff",
            "Pending handoff processing finished.",
            processed_count=processed,
            failed_count=failed,
        )
        return self.send(200, data)

    def do_POST(self):
        if not self.allowed() or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.send(403, {"error": "Local requests only"})
        if not self.path.startswith("/runtime/"):
            return self.proxy()
        runtime_path = urllib.parse.urlsplit(self.path).path
        try:
            if runtime_path == "/runtime/handoff/upload":
                return self._handle_handoff_upload()
            if runtime_path == "/runtime/handoff/process":
                return self._handle_handoff_process()
        except (OSError, ValueError, http.client.HTTPException) as exc:
            self.server.runtime.event(
                "error",
                "handoff",
                f"{type(exc).__name__}: {exc}",
                path=runtime_path,
            )
            return self.send(
                502 if isinstance(exc, (OSError, http.client.HTTPException)) else 400,
                {"error": self.server.runtime.redact(str(exc))},
            )
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
                        ("KATCHA_CONVERSATION_PROVIDER", ("gemini", "codex", "auto")),
                        ("KATCHA_AGENT_PROVIDER", ("codex", "gemini", "auto")),
                        ("KATCHA_ALLOW_PAID_OPENAI_FALLBACK", ("true", "false")),
                    ]:
                        if key in body and body[key] not in choices:
                            raise ValueError(f"Invalid {key}")
                    if body.get("KATCHA_AI_EXECUTION_MODE") == "live":
                        # The launcher exposes one Live choice. Apply its enablement too;
                        # otherwise a saved AI_ENABLED=false silently keeps chat in fallback.
                        body["KATCHA_AI_ENABLED"] = "true"
                    runtime.save(body)
                    runtime.event("info", "setup", "Settings saved; Start applies changes")
                finally:
                    runtime.lock.release()
                return self.send(200, {"ok": True})
            if self.path == "/runtime/ai/apply":
                if not runtime.lock.acquire(blocking=False):
                    return self.send(409, {"error": "Wait for the active operation"})
                try:
                    applied = runtime.apply_ai()
                finally:
                    runtime.lock.release()
                return self.send(200, {"ok": True, "applied": applied})
            if self.path == "/runtime/telegram/apply":
                if not runtime.lock.acquire(blocking=False):
                    return self.send(409, {"error": "Wait for the active operation"})
                try:
                    applied = runtime.apply_telegram()
                finally:
                    runtime.lock.release()
                return self.send(200, {"ok": True, "applied": applied})
            action = self.path.removeprefix("/runtime/")
            if action not in ("start", "stop"):
                return self.send(404, {"error": "Unknown action"})
            return self.send(202 if runtime.operate(action) else 409, {"action": action})
        except (ValueError, OSError) as exc:
            runtime.event("error", "setup", str(exc))
            return self.send(400, {"error": runtime.redact(exc)})

    def proxy(self):
        allowed_prefixes = (
            "/v1/",
            "/home",
            "/editing",
            "/explorer",
            "/ingestion",
            "/clips",
            "/channels",
            "/studio",
            "/ai",
            "/operations",
            "/settings",
            "/auth/callback",
        )
        if not self.path.startswith(allowed_prefixes):
            return self.send(404, {"error": "Not found"})
        connection = None
        headers_sent = False
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                8000,
                timeout=120,
            )
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 11_000_000:
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
        except Exception as exc:
            self.server.runtime.event(
                "error",
                "gateway",
                f"{type(exc).__name__}: {exc}",
                method=self.command,
                path=self.path.split("?")[0],
            )
            if not headers_sent:
                self.send(
                    502,
                    {
                        "error": (
                            "The local Katcha gateway could not complete this request. "
                            "Open the launch console for the recorded gateway error."
                        )
                    },
                )
            else:
                self.close_connection = True
        finally:
            if connection is not None:
                connection.close()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def _is_wsl():
    path = Path("/proc/sys/kernel/osrelease")
    try:
        return path.exists() and "microsoft" in path.read_text().lower()
    except OSError:
        return False


def _open_browser(url, runtime):
    """Open the local launcher in the host browser, with WSL-safe fallbacks."""
    errors = []
    if _is_wsl():
        attempts = [
            (
                "powershell",
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Start-Process '{url}'",
                ],
            ),
            ("cmd", ["cmd.exe", "/d", "/c", "start", "", url]),
            ("explorer", ["explorer.exe", url]),
        ]
        for method, command in attempts:
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{method}: {exc}")
                continue
            if result.returncode == 0:
                runtime.event(
                    "info",
                    "browser",
                    "Opened Katcha in the host browser.",
                    method=method,
                )
                return True
            detail = (result.stderr or "").strip()
            errors.append(
                f"{method}: exit {result.returncode}"
                + (f" ({detail})" if detail else "")
            )

    try:
        if webbrowser.open(url, new=2):
            runtime.event(
                "info",
                "browser",
                "Opened Katcha in the default browser.",
                method="python",
            )
            return True
        errors.append("python: no runnable browser was reported")
    except (OSError, webbrowser.Error) as exc:
        errors.append(f"python: {exc}")

    runtime.event(
        "warning",
        "browser",
        "Katcha could not open a browser automatically.",
        attempts=errors,
        recovery="Open " + url,
    )
    print("Katcha could not open a browser automatically. Open " + url)
    return False


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
    url = "http://127.0.0.1:8765"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    except OSError:
        print("Port 8765 is in use. If Katcha is already running, open " + url)
        return 1
    runtime = Runtime()
    server.runtime = runtime
    runtime.event("info", "launcher", "Launch console listening on " + url)
    runtime.monitor_thread = threading.Thread(
        target=runtime.monitor,
        daemon=True,
        name="katcha-supervisor",
    )
    runtime.monitor_thread.start()
    if (args.auto_start or runtime.desired_running) and not args.no_start:
        runtime.operate("start")
    elif not args.no_start:
        threading.Thread(target=runtime.reconcile_existing, daemon=True).start()
    def open_when_listening():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=1)
            try:
                connection.request("GET", "/runtime/status")
                if connection.getresponse().status == 200:
                    break
            except (OSError, http.client.HTTPException):
                time.sleep(0.1)
            finally:
                connection.close()
        else:
            runtime.event(
                "warning",
                "browser",
                "Launcher did not answer its local readiness probe before browser open.",
                recovery="Open " + url,
            )
            print("Katcha browser readiness timed out. Open " + url)
            return

        _open_browser(url, runtime)

    if not args.no_browser:
        threading.Thread(target=open_when_listening, daemon=True).start()
    print("Katcha: " + url + " — close with Ctrl+C; services remain running.")
    interrupted = False
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        interrupted = True
        # Ignore repeated Ctrl+C while the launcher-owned log follower is being
        # reaped. Katcha services intentionally remain running.
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
    finally:
        runtime.shutdown()
        server.server_close()
    if interrupted:
        print("Katcha launcher closed; services remain running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
