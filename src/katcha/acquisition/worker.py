"""Standalone acquisition host. Run with ``python -m katcha.acquisition.worker``.

The host has one channel-scoped API token, no database/Temporal/bucket secrets.
A private durable journal is the execution checkpoint; uncertain provider work is
never blindly re-executed. Multipart recovery does not repeat the provider request.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import logging
import os
import shutil
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from katcha.acquisition.fetch import FetchFailure, fetch_extracted, fetch_file

log = logging.getLogger("katcha.acquisition.worker")


class LeaseLost(Exception):
    pass


class Journal:
    def __init__(self, directory: Path, channel: str, job_id: str):
        self.directory = directory / str(uuid.UUID(channel)) / str(uuid.UUID(job_id))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "state.json"
        self.lock = threading.RLock()
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "channel": channel,
                "job_id": job_id,
                "claim_id": str(uuid.uuid4()),
                "phase": "new",
                "sequence": 0,
                "bytes_received": 0,
            }
        )
        self.save()

    def save(self):
        with self.lock:
            temp = self.path.with_suffix(".tmp")
            fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(self.state, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
            fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def update(self, **values):
        with self.lock:
            self.state.update(values)
            self.save()

    def identity(self):
        return {"claim_id": self.state["claim_id"], "generation": self.state["generation"]}


class Worker:
    def __init__(
        self,
        api_url,
        token,
        channels,
        state_dir: Path,
        *,
        client=None,
        storage_client=None,
        cookie_file=None,
        access_headers=None,
        control_ca_file=None,
    ):
        parsed = urlsplit(api_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Worker control URL must be a trusted HTTPS origin")
        if not token.strip():
            raise ValueError("A dedicated worker token is required")
        self.api_url = api_url.rstrip("/")
        self.channels = [str(uuid.UUID(channel)) for channel in channels]
        if not self.channels:
            raise ValueError("At least one explicit channel is required")
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30, connect=5), trust_env=False, follow_redirects=False,
            verify=ssl.create_default_context(cafile=control_ca_file),
        )
        # Separate clients: bearer authority must NEVER reach a source or signed storage URL.
        self.storage_client = storage_client or httpx.Client(
            timeout=httpx.Timeout(60, connect=5), trust_env=False, follow_redirects=False
        )
        access_headers = access_headers or {}
        if set(access_headers) - {"CF-Access-Client-Id", "CF-Access-Client-Secret"}:
            raise ValueError("Only configured Access service headers are accepted")
        self.headers = {**access_headers, "Authorization": f"Bearer {token}"}
        self.cookie_file = Path(cookie_file) if cookie_file else None
        if self.cookie_file and self.cookie_file.stat().st_mode & 0o077:
            raise ValueError("Operator cookie file must be readable only by its owner")

    def request(self, method, path, payload=None):
        # All control mutations carry persistent identities and are safe to retry verbatim.
        for attempt in range(3):
            try:
                response = self.client.request(
                    method, self.api_url + path, json=payload, headers=self.headers
                )
                if response.is_redirect:
                    raise LeaseLost(
                        "Control origin redirected; configure API Access service credentials"
                    )
                if response.status_code in {401, 403, 404, 409, 410}:
                    raise LeaseLost(f"Control request refused ({response.status_code})")
                if response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        raise RuntimeError("Unreachable retry state")

    @staticmethod
    def root(channel):
        return f"/v1/channels/{channel}/acquisition-jobs"

    def run_job(self, journal: Journal):
        state = journal.state
        path = f"{self.root(state['channel'])}/{state['job_id']}"
        current = self.request("GET", path)
        if current["status"] in {
            "completed",
            "awaiting_validation",
            "failed",
            "cancelled",
        }:
            journal.update(phase="done", server_status=current["status"])
            if current["status"] in {"completed", "awaiting_validation"}:
                self._remove_media(journal)
            return
        if current["status"] == "recovery_required" and not (
            current.get("failure_code") == "worker_lost"
            and state["phase"] in {"acquired", "uploading"}
            and state.get("sha256")
        ):
            journal.update(phase="done", server_status="recovery_required")
            return
        # A prior process may have downloaded the complete file before persisting its hash.
        # Only a completed local file with a durable identity can bypass provider access.
        partial = journal.directory / "media.part"
        checkpoint = state.get("download", {})
        if (
            state["phase"] == "acquiring"
            and state.get("transport") == "direct"
            and checkpoint.get("total")
            and partial.is_file()
            and partial.stat().st_size == checkpoint["total"]
        ):
            journal.update(
                phase="acquired",
                filename=partial.name,
                sha256=sha256_file(partial),
                size_bytes=partial.stat().st_size,
                bytes_received=partial.stat().st_size,
            )
        recovering = state["phase"] in {"acquired", "uploading"} and state.get("sha256")
        if recovering:
            media = journal.directory / state["filename"]
            if (
                not media.is_file()
                or media.stat().st_size != state["size_bytes"]
                or sha256_file(media) != state["sha256"]
            ):
                raise LeaseLost(
                    "Journal media is missing or changed; operator reconciliation required"
                )
        try:
            lease = self.request("POST", path + "/claim", {"claim_id": state["claim_id"]})
        except LeaseLost:
            if not recovering:
                raise
            lease = self.request(
                "POST",
                path + "/upload/recover",
                {
                    **journal.identity(),
                    "sha256": state["sha256"],
                    "size_bytes": state["size_bytes"],
                },
            )
        journal.update(
            generation=lease["job"]["generation"],
            sequence=max(state["sequence"], lease["job"]["heartbeat_sequence"]),
            bytes_received=max(state["bytes_received"], lease["job"]["bytes_received"]),
        )
        stop, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not stop.is_set():
                with journal.lock:
                    sequence = state["sequence"] + 1
                    phase = (
                        "uploading" if state["phase"] in {"acquired", "uploading"} else "acquiring"
                    )
                    message = {
                        **journal.identity(),
                        "sequence": sequence,
                        "phase": phase,
                        "bytes_received": state["bytes_received"],
                    }
                    journal.update(sequence=sequence)
                try:
                    self.request("POST", path + "/heartbeat", message)
                except Exception:
                    lost.set()
                    return
                stop.wait(15)

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()

        def progress(size):
            if lost.is_set():
                raise LeaseLost("Lease heartbeat unavailable; provider work stopped")
            with journal.lock:
                # Extractor byte totals include separate tracks; final media size is measured
                # after merging. Do not confuse provider traffic with canonical asset length.
                state["download_bytes"] = size
                if state.get("transport") == "direct":
                    state["bytes_received"] = max(size, state["bytes_received"])
                journal.save()

        try:
            if not recovering:
                from katcha.integrations.download import detect_platform

                direct = detect_platform(lease["fetch_url"]) == "generic"
                if state["phase"] == "acquiring" and (
                    not direct or not state.get("download", {}).get("etag") or not partial.is_file()
                ):
                    raise FetchFailure("session_lost")
                if (
                    shutil.disk_usage(journal.directory).free
                    < lease["job"]["max_bytes"] * (1 if direct else 2) + 256 * 1024**2
                ):
                    raise FetchFailure("size_limit")
                journal.update(phase="acquiring", transport="direct" if direct else "extractor")
                if direct:
                    checkpoint = state.setdefault("download", {})
                    media = fetch_file(
                        lease["fetch_url"],
                        lease["permission"],
                        journal.directory / "media.part",
                        lease["job"]["max_bytes"],
                        checkpoint,
                        progress,
                    )
                else:
                    cookie_copy = journal.directory / "session.cookies"
                    try:
                        if self.cookie_file:
                            shutil.copyfile(self.cookie_file, cookie_copy)
                            cookie_copy.chmod(0o600)
                        media = fetch_extracted(
                            lease["fetch_url"],
                            lease["permission"],
                            journal.directory,
                            lease["job"]["max_bytes"],
                            progress,
                            cookie_file=cookie_copy if self.cookie_file else None,
                        )
                    finally:
                        cookie_copy.unlink(missing_ok=True)
                journal.update(
                    phase="acquired",
                    filename=media.name,
                    sha256=sha256_file(media),
                    size_bytes=media.stat().st_size,
                    bytes_received=media.stat().st_size,
                )
            if lost.is_set():
                raise LeaseLost("Lease heartbeat unavailable")
            journal.update(phase="uploading")
            manifest = self.request(
                "POST",
                path + "/upload",
                {
                    **journal.identity(),
                    "sha256": state["sha256"],
                    "size_bytes": state["size_bytes"],
                },
            )
            if not manifest["completed"]:
                uploaded = set(manifest["uploaded_parts"])
                with media.open("rb") as source:
                    number = 0
                    while data := source.read(manifest["part_bytes"]):
                        number += 1
                        if lost.is_set():
                            raise LeaseLost("Lease heartbeat unavailable")
                        if number in uploaded:
                            continue
                        md5 = base64.b64encode(
                            hashlib.md5(data, usedforsecurity=False).digest()
                        ).decode()
                        # Refresh grants when retrying a part, since a previous URL may expire.
                        for attempt in range(3):
                            grant = self.request(
                                "POST",
                                path + f"/upload/parts/{number}",
                                {**journal.identity(), "content_md5": md5},
                            )
                            parsed = urlsplit(grant["url"])
                            if parsed.scheme != "https" or parsed.username or parsed.password:
                                raise LeaseLost("Storage grant must use HTTPS")
                            try:
                                response = self.storage_client.put(
                                    grant["url"], content=data, headers=grant["headers"]
                                )
                                response.raise_for_status()
                                break
                            except (httpx.TransportError, httpx.HTTPStatusError):
                                if attempt == 2:
                                    raise
                        journal.update(
                            uploaded_bytes=min(number * manifest["part_bytes"], state["size_bytes"])
                        )
            result = self.request("POST", path + "/upload/complete", journal.identity())
            journal.update(phase="done", server_status=result["status"])
            self._remove_media(journal)
        except FetchFailure as exc:
            journal.update(failure_code=exc.code)
            self.request("POST", path + "/fail", {**journal.identity(), "code": exc.code})
            journal.update(phase="done", server_status="failed")
            if exc.code not in {"transport_error", "session_lost", "outcome_unknown"}:
                self._remove_media(journal)
        except (httpx.TransportError, httpx.HTTPStatusError):
            # Preserve the journal/upload cursor. The next run resumes upload or an ETag-bound
            # partial file; it never converts an uncertain control response into a new claim.
            log.warning("Acquisition connection interrupted job=%s", state["job_id"])
        finally:
            stop.set()
            thread.join(timeout=35)

    @staticmethod
    def _remove_media(journal):
        for path in journal.directory.glob("media.*"):
            if path.is_file():
                path.unlink()

    def once(self):
        seen = set()
        for path in sorted(self.state_dir.glob("*/*/state.json")):
            channel, job_id = path.parent.parent.name, path.parent.name
            if channel not in self.channels:
                continue
            journal = Journal(self.state_dir, channel, job_id)
            if journal.state["phase"] != "done":
                seen.add(job_id)
                try:
                    self.run_job(journal)
                except LeaseLost as exc:
                    log.warning("Acquisition requires reconciliation job=%s reason=%s", job_id, exc)
        for channel in self.channels:
            queued = self.request("GET", self.root(channel) + "?status=queued&limit=100")
            for job in queued:
                if job["id"] in seen:
                    continue
                try:
                    self.run_job(Journal(self.state_dir, channel, job["id"]))
                except LeaseLost as exc:
                    log.warning("Acquisition claim unavailable job=%s reason=%s", job["id"], exc)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Channel-scoped Katcha acquisition worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx request logs include signed source/storage URLs. Keep them private.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token_path = Path(os.environ["KATCHA_ACQUISITION_TOKEN_FILE"])
    if token_path.stat().st_mode & 0o077:
        raise ValueError("Worker token file must be readable only by its owner")
    access_headers = {}
    for suffix, header in [("ID", "CF-Access-Client-Id"), ("SECRET", "CF-Access-Client-Secret")]:
        filename = os.environ.get(f"KATCHA_ACQUISITION_CF_ACCESS_{suffix}_FILE")
        if filename:
            path = Path(filename)
            if path.stat().st_mode & 0o077:
                raise ValueError("Access service credential files must be private")
            access_headers[header] = path.read_text().strip()
    if access_headers and len(access_headers) != 2:
        raise ValueError("Configure both Access service credential files")
    worker = Worker(
        os.environ["KATCHA_ACQUISITION_API_URL"],
        token_path.read_text().strip(),
        os.environ["KATCHA_ACQUISITION_CHANNEL_IDS"].split(","),
        Path(os.environ.get("KATCHA_ACQUISITION_STATE_DIR", "/var/lib/katcha-acquisition")),
        cookie_file=os.environ.get("KATCHA_ACQUISITION_COOKIE_FILE"),
        access_headers=access_headers,
        control_ca_file=os.environ.get("KATCHA_ACQUISITION_CONTROL_CA_FILE"),
    )
    lock = (worker.state_dir / "worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    while True:
        try:
            worker.once()
        except Exception as exc:
            # No exception message: upstream exceptions can contain bearer/signed URLs.
            log.error("Worker pass interrupted type=%s", type(exc).__name__)
        if args.once:
            break
        time.sleep(5)


if __name__ == "__main__":
    main()
