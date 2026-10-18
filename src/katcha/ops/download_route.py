"""Private SSH SOCKS helper managed within the ingest worker lifecycle."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import socket
import stat
import sys
from pathlib import Path

KEY = Path("/run/secrets/download_route_key")
KNOWN_HOSTS = Path("/run/secrets/download_route_known_hosts")
LISTEN_PORT = 18081


def command(environment: dict[str, str], key: Path = KEY,
            known_hosts: Path = KNOWN_HOSTS) -> list[str]:
    host = environment.get("KATCHA_DOWNLOAD_ROUTE_HOST", "")
    user = environment.get("KATCHA_DOWNLOAD_ROUTE_USER", "")
    port = environment.get("KATCHA_DOWNLOAD_ROUTE_PORT", "22")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        raise ValueError("A download host DNS name or IPv4 address is required")
    if user == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("A dedicated non-root SSH user is required")
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("The download host SSH port must be between 1 and 65535")
    if not key.is_file() or not known_hosts.is_file():
        raise ValueError("Mount the dedicated key and independently verified known-hosts file")
    if key.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("The download host key must not be accessible to group or other users")
    if not key.stat().st_size or not known_hosts.stat().st_size:
        raise ValueError("The dedicated key and known-hosts files must not be empty")
    return [
        "ssh", "-F", "/dev/null", "-N", "-T",
        "-D", f"127.0.0.1:{LISTEN_PORT}",
        "-i", str(key), "-p", port,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "HostKeyAlias=katcha-download-route", "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3", "-o", "LogLevel=ERROR", f"{user}@{host}",
    ]


def healthy() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", LISTEN_PORT), timeout=2) as stream:
            stream.sendall(b"\x05\x01\x00")
            # TCP reads may split even a two-byte SOCKS response.
            response = b""
            while len(response) < 2:
                chunk = stream.recv(2 - len(response))
                if not chunk:
                    return False
                response += chunk
            return response == b"\x05\x00"
    except OSError:
        return False


def enabled_from_file(path: Path) -> bool:
    values = []
    for line in path.read_text().splitlines():
        key, separator, value = line.strip().partition("=")
        if key.strip() == "KATCHA_DOWNLOAD_ROUTE_ENABLED":
            if not separator:
                raise ValueError("Malformed download-route enablement")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '\"'}:
                value = value[1:-1]
            values.append(value)
    if not values:
        return False
    if len(values) != 1 or values[0] not in {"true", "false"}:
        raise ValueError("Download-route enablement must be one explicit true or false")
    return values[0] == "true"


async def maintain(argv: list[str]) -> None:
    delay = 1
    while True:
        process = None
        started = asyncio.get_running_loop().time()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        except OSError:
            pass
        finally:
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        lifetime = asyncio.get_running_loop().time() - started
        if lifetime > 60:
            delay = 1
        logging.getLogger(__name__).warning(
            "Download route disconnected; reconnecting in %s seconds", delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


@contextlib.asynccontextmanager
async def managed_route():
    enabled = os.environ.get("KATCHA_DOWNLOAD_ROUTE_ENABLED", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("Download-route enablement must be true or false")
    if enabled == "false":
        yield
        return
    argv = command(dict(os.environ))
    if os.environ.get("KATCHA_YOUTUBE_DOWNLOAD_PROXY_URL") != "socks5://127.0.0.1:18081":
        raise ValueError("The managed download route requires its loopback SOCKS address")
    task = asyncio.create_task(maintain(argv), name="download-route")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--enabled":
        try:
            print("true" if enabled_from_file(Path(sys.argv[2])) else "false")
            return 0
        except (OSError, ValueError):
            print("Download-route enablement configuration invalid", file=sys.stderr)
            return 2
    if sys.argv[1:] == ["--healthcheck"]:
        return 0 if healthy() else 1
    if sys.argv[1:] not in ([], ["--check"]):
        print("Usage: python -m katcha.ops.download_route [--check|--healthcheck]", file=sys.stderr)
        return 2
    try:
        argv = command(dict(os.environ))
    except (OSError, ValueError):
        print("Download route configuration invalid; check host, user and private key mounts",
              file=sys.stderr)
        return 2
    if sys.argv[1:] == ["--check"]:
        print("Download route configuration valid; connectivity and media access not tested")
        return 0
    os.execvp(argv[0], argv)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
