"""Network-boundary and secret-handling contracts for the browser RPC."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from katcha.editorial import retrieval
from katcha.services import server_browser


class Writer:
    def __init__(self):
        self.output = bytearray()
        self.closed = False

    def write(self, data):
        self.output.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def dns_answer(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses]


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1",
                                     "::1", "fc00::1", "224.0.0.1"])
def test_dns_rejects_private_answer_even_with_public_answer(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw:
                        dns_answer("93.184.216.34", address))
    with pytest.raises(ValueError, match="non-public"):
        retrieval.public_addresses("https://fixture.example/")


@pytest.mark.parametrize("target", ["localhost:80", "localhost:443", "169.254.169.254:443",
                                    "user:password@example.com:443"])
async def test_connect_rejects_unsafe_targets_without_opening_upstream(monkeypatch, target):
    opened = []

    async def forbidden_connect(*args, **kwargs):
        opened.append(args)
        raise AssertionError("unsafe upstream connection")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: dns_answer("127.0.0.1"))
    monkeypatch.setattr(asyncio, "open_connection", forbidden_connect)
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    reader.feed_eof()
    proxy = server_browser.PublicProxy()
    await proxy.handle(reader, writer)
    assert not opened
    assert b"403 Forbidden" in writer.output
    assert writer.closed
    assert not proxy.writers


async def test_connect_pins_public_literal_and_closes_tunnel(monkeypatch):
    opened = []
    upstream_reader, upstream_writer = asyncio.StreamReader(), Writer()
    upstream_reader.feed_eof()

    async def connect(address, port):
        opened.append((address, port))
        return upstream_reader, upstream_writer

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: dns_answer("93.184.216.34"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(b"CONNECT fixture.example:443 HTTP/1.1\r\n\r\n")
    reader.feed_eof()
    proxy = server_browser.PublicProxy()
    await proxy.handle(reader, writer)
    assert opened == [("93.184.216.34", 443)]
    assert b"200 Connection Established" in writer.output
    assert writer.closed and upstream_writer.closed
    assert not proxy.writers
    assert not proxy.tasks


@pytest.mark.parametrize("token", ["", "short"])
async def test_unconfigured_service_rejects_even_matching_auth(monkeypatch, token):
    monkeypatch.setenv("KATCHA_BROWSER_SERVICE_TOKEN", token)
    request = server_browser.BrowserRequest(scope=("actor", "channel", "goal"),
                                           arguments={"action": "snapshot"})
    with pytest.raises(HTTPException) as exc:
        await server_browser.execute(request, "Bearer " + token)
    assert exc.value.status_code == 401


async def test_browser_failure_does_not_echo_form_secrets(monkeypatch):
    token = "a" * 32
    monkeypatch.setenv("KATCHA_BROWSER_SERVICE_TOKEN", token)

    async def fail(*args):
        raise RuntimeError("password=supersecret; https://user:secret@fixture.example")

    monkeypatch.setattr(server_browser.pool, "execute", fail)
    request = server_browser.BrowserRequest(scope=("actor", "channel", "goal"),
                                           arguments={"action": "fill", "ref": "e0",
                                                      "text": "supersecret"})
    result = await server_browser.execute(request, "Bearer " + token)
    assert result["code"] == "browser_action_failed"
    assert result["outcome_uncertain"] is True
    assert "secret" not in str(result)


async def test_proxy_shutdown_cancels_queued_connections():
    proxy = server_browser.PublicProxy()
    # A queued handler must not gain the semaphore and connect after shutdown.
    proxy.connections = asyncio.Semaphore(0)
    reader, writer = asyncio.StreamReader(), Writer()
    handler = asyncio.create_task(proxy.handle(reader, writer))
    await asyncio.sleep(0)
    assert handler in proxy.tasks
    await proxy.close()
    assert handler.done()
    assert writer.closed
    assert not proxy.tasks
    assert not proxy.writers
    # Connections accepted just as the listener closes also fail closed.
    proxy.connections.release()
    second = Writer()
    await proxy.handle(asyncio.StreamReader(), second)
    assert second.closed


async def test_snapshots_never_reassign_old_reference_to_another_element():
    first, second = object(), object()
    item = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                           evaluate=AsyncMock(return_value={"tag": "button", "name": "Submit"}),
                           element_handle=AsyncMock(side_effect=[first, second]))
    controls = SimpleNamespace(all=AsyncMock(return_value=[item]))
    body = SimpleNamespace(inner_text=AsyncMock(return_value="A page"))
    page = SimpleNamespace(frames=[SimpleNamespace(locator=lambda _: controls)],
                           locator=lambda _: body, url="https://fixture.example/",
                           title=AsyncMock(return_value="Fixture"))
    session = server_browser.Session(SimpleNamespace(pages=[page]), None, page=page)
    pool = server_browser.BrowserPool()
    old = await pool.snapshot(session)
    current = await pool.snapshot(session)
    old_ref = old["elements"][0]["ref"]
    new_ref = current["elements"][0]["ref"]
    assert old_ref != new_ref
    assert old_ref not in session.refs
    assert session.refs[new_ref] is second


async def test_action_waiting_for_session_lock_cannot_reuse_closed_context():
    pool = server_browser.BrowserPool()
    context = SimpleNamespace(close=AsyncMock())
    proxy = SimpleNamespace(close=AsyncMock())
    session = server_browser.Session(context, proxy)
    key = ("actor", "channel", "goal")
    pool.sessions[key] = session
    pool.browser = SimpleNamespace(is_connected=lambda: True)
    await session.lock.acquire()
    waiting = asyncio.create_task(
        pool.execute(key, server_browser.BrowserAction(action="snapshot"))
    )
    await asyncio.sleep(0)
    await pool.close_session(key)
    session.lock.release()
    result = await waiting
    assert result["code"] == "session_expired"
    assert result["available"] is False
    context.close.assert_awaited_once()
    proxy.close.assert_awaited_once()


@pytest.mark.parametrize("action", ["mouse_click", "drag", "type", "press", "screenshot"])
async def test_stale_view_cannot_target_focused_controls_or_capture_new_page(action):
    pool = server_browser.BrowserPool()
    key = ("actor", "channel", "goal")
    page = SimpleNamespace(
        url="https://fixture.example/", is_closed=lambda: False,
        mouse=SimpleNamespace(click=AsyncMock(), move=AsyncMock()),
        keyboard=SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock()),
        screenshot=AsyncMock(),
    )
    session = server_browser.Session(SimpleNamespace(), None, page=page,
                                    snapshot_id="current-view", snapshot_url=page.url)
    pool.sessions[key] = session
    pool.browser = SimpleNamespace(is_connected=lambda: True)
    with pytest.raises(ValueError, match="Refresh"):
        await pool.execute(key, server_browser.BrowserAction(action=action,
                                                             snapshot_id="stale-view"))
    page.mouse.click.assert_not_awaited()
    page.mouse.move.assert_not_awaited()
    page.keyboard.insert_text.assert_not_awaited()
    page.keyboard.press.assert_not_awaited()
    page.screenshot.assert_not_awaited()
