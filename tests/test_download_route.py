from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from katcha.ops import download_route as route


def files(tmp_path):
    key, hosts = tmp_path / "key", tmp_path / "hosts"
    key.write_text("offline fixture key")
    key.chmod(0o600)
    hosts.write_text("katcha-download-route ssh-ed25519 fixture")
    return key, hosts


def test_route_uses_pinned_host_and_loopback_only(tmp_path):
    key, hosts = files(tmp_path)
    argv = route.command({"KATCHA_DOWNLOAD_ROUTE_HOST": "download.example.com",
                          "KATCHA_DOWNLOAD_ROUTE_USER": "katcha-download"}, key, hosts)
    assert "127.0.0.1:18081" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "IdentityAgent=none" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "HostKeyAlias=katcha-download-route" in argv
    assert argv[-1] == "katcha-download@download.example.com"
    assert "offline fixture key" not in str(argv)
    key.chmod(0o644)
    with pytest.raises(ValueError, match="accessible"):
        route.command({"KATCHA_DOWNLOAD_ROUTE_HOST": "host",
                       "KATCHA_DOWNLOAD_ROUTE_USER": "user"}, key, hosts)


@pytest.mark.parametrize("field,value", [
    ("HOST", "-oProxyCommand=evil"), ("HOST", "host;evil"),
    ("USER", "root"), ("USER", "user@host"), ("PORT", "65536"), ("PORT", "22 -v"),
])
def test_route_rejects_invalid_destination(tmp_path, field, value):
    key, hosts = files(tmp_path)
    env = {"KATCHA_DOWNLOAD_ROUTE_HOST": "host", "KATCHA_DOWNLOAD_ROUTE_USER": "user"}
    env["KATCHA_DOWNLOAD_ROUTE_" + field] = value
    with pytest.raises(ValueError):
        route.command(env, key, hosts)


@pytest.mark.parametrize("text,expected", [
    ("SECRET=not-exposed\n", False),
    ("KATCHA_DOWNLOAD_ROUTE_ENABLED=true\n", True),
    ("KATCHA_DOWNLOAD_ROUTE_ENABLED='false'\n", False),
])
def test_enablement_is_explicit(tmp_path, text, expected):
    path = tmp_path / "env"
    path.write_text(text)
    assert route.enabled_from_file(path) is expected


def test_duplicate_or_unknown_enablement_is_rejected(tmp_path):
    path = tmp_path / "env"
    for text in ("KATCHA_DOWNLOAD_ROUTE_ENABLED=yes",
                 "KATCHA_DOWNLOAD_ROUTE_ENABLED=true\nKATCHA_DOWNLOAD_ROUTE_ENABLED=false"):
        path.write_text(text)
        with pytest.raises(ValueError):
            route.enabled_from_file(path)


async def test_disabled_route_does_not_start_ssh(monkeypatch):
    monkeypatch.delenv("KATCHA_DOWNLOAD_ROUTE_ENABLED", raising=False)
    start = AsyncMock()
    monkeypatch.setattr(route.asyncio, "create_subprocess_exec", start)
    async with route.managed_route():
        pass
    start.assert_not_called()


async def test_worker_shutdown_terminates_route(monkeypatch):
    ready, stopped = asyncio.Event(), asyncio.Event()

    class Child:
        returncode = None

        async def wait(self):
            await stopped.wait()
            return self.returncode

        def terminate(self):
            self.returncode = 0
            stopped.set()

    child = Child()

    async def start(*args, **kwargs):
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        ready.set()
        return child

    monkeypatch.setattr(route, "command", lambda env: ["ssh", "fixture"])
    monkeypatch.setattr(route.asyncio, "create_subprocess_exec", start)
    monkeypatch.setenv("KATCHA_DOWNLOAD_ROUTE_ENABLED", "true")
    monkeypatch.setenv("KATCHA_YOUTUBE_DOWNLOAD_PROXY_URL", "socks5://127.0.0.1:18081")
    async with route.managed_route():
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert child.returncode is None
    assert stopped.is_set()


async def test_disconnected_route_reconnects_with_backoff(monkeypatch):
    delays = []
    clock = [0.0]
    monkeypatch.setattr(route.asyncio, "get_running_loop",
                        lambda: SimpleNamespace(time=lambda: clock[0]))
    child = type("Child", (), {"returncode": 255, "wait": AsyncMock(return_value=255)})()
    start = AsyncMock(return_value=child)

    async def sleep(delay):
        delays.append(delay)
        clock[0] += delay
        if len(delays) == 9:
            raise asyncio.CancelledError

    monkeypatch.setattr(route.asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(route.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await route.maintain(["ssh", "fixture"])
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert start.await_count == 9


def test_health_requires_socks_negotiation(monkeypatch):
    class Stream:
        replies = iter([b"\x05", b"\x00"])
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def sendall(self, value):
            assert value == b"\x05\x01\x00"
        def recv(self, size):
            return next(self.replies)
    monkeypatch.setattr(route.socket, "create_connection", lambda *a, **kw: Stream())
    assert route.healthy()
