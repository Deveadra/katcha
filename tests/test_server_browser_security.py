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



@pytest.mark.parametrize(
    ("action", "message", "code", "stage"),
    [
        ("open", "Research source resolved to a non-public address",
         "browser_destination_rejected", "navigation"),
        ("open", "Research sources must be public HTTPS pages without credentials",
         "browser_destination_rejected", "navigation"),
        ("click", "Refresh the page view and choose the control again.",
         "browser_stale_control", "interaction"),
        ("type", "Refresh the page view before clicking or using the keyboard",
         "browser_stale_snapshot", "interaction"),
        ("switch_tab", "That tab has closed. Refresh the page view.",
         "browser_tab_closed", "interaction"),
        ("open", "All browser sessions are busy. Close a session and try again.",
         "browser_capacity_exhausted", "navigation"),
    ],
)
async def test_browser_known_precondition_failures_are_actionable(
    monkeypatch, caplog, action, message, code, stage
):
    token = "a" * 32
    monkeypatch.setenv("KATCHA_BROWSER_SERVICE_TOKEN", token)

    async def fail(*args):
        raise ValueError(message)

    monkeypatch.setattr(server_browser.pool, "execute", fail)
    request = server_browser.BrowserRequest(
        scope=("actor", "channel", "goal"),
        arguments={"action": action, **(
            {"url": "https://fixture.example/"} if action == "open"
            else {"ref": "e1"} if action == "click" else {}
        )},
    )
    result = await server_browser.execute(request, "Bearer " + token)
    assert result["code"] == code
    assert result["stage"] == stage
    assert result["outcome_uncertain"] is False
    assert len(result["diagnostic_id"]) == 32
    assert result["diagnostic_id"] in caplog.text
    assert "actor" not in caplog.text


@pytest.mark.parametrize(("action", "uncertain"), [
    ("snapshot", False), ("click", True), ("open", True),
])
async def test_browser_timeout_retains_uncertainty_without_leaking_values(
    monkeypatch, caplog, action, uncertain
):
    token = "a" * 32
    monkeypatch.setenv("KATCHA_BROWSER_SERVICE_TOKEN", token)

    async def fail(*args):
        raise TimeoutError("password=topsecret; https://user:topsecret@host.example")

    monkeypatch.setattr(server_browser.pool, "execute", fail)
    request = server_browser.BrowserRequest(
        scope=("actor", "channel", "goal"),
        arguments={"action": action, **(
            {"url": "https://fixture.example/"} if action == "open"
            else {"ref": "e1"} if action == "click" else {}
        )},
    )
    result = await server_browser.execute(request, "Bearer " + token)
    assert result["code"] == "browser_timeout"
    assert result["outcome_uncertain"] is uncertain
    assert "topsecret" not in str(result)
    assert "topsecret" not in caplog.text
    assert result["diagnostic_id"] in caplog.text


async def test_browser_unknown_failure_uses_safe_trace_not_raw_exception(
    monkeypatch, caplog
):
    token = "a" * 32
    monkeypatch.setenv("KATCHA_BROWSER_SERVICE_TOKEN", token)

    async def fail(*args):
        raise ValueError("password=topsecret https://u:topsecret@host.example")

    monkeypatch.setattr(server_browser.pool, "execute", fail)
    request = server_browser.BrowserRequest(
        scope=("actor", "channel", "goal"),
        arguments={"action": "click", "ref": "e1"},
    )
    result = await server_browser.execute(request, "Bearer " + token)
    assert result["code"] == "browser_action_failed"
    assert result["outcome_uncertain"] is True
    assert "topsecret" not in str(result)
    assert "topsecret" not in caplog.text
    assert result["diagnostic_id"] in caplog.text


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


async def test_connect_budget_exhaustion_fails_before_any_dns_or_upstream(monkeypatch):
    monkeypatch.setattr(server_browser, "MAX_BROWSER_CONNECTS", 0)
    opened = []

    async def forbidden(*args, **kwargs):
        opened.append(args)
        raise AssertionError("No outgoing connection allowed")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(b"CONNECT fixture.example:443 HTTP/1.1\\r\\n\\r\\n")
    reader.feed_eof()
    proxy = server_browser.PublicProxy()
    await proxy.handle(reader, writer)
    assert proxy.exhausted is True
    assert proxy.connect_attempts == 1
    assert not opened
    assert b"403 Forbidden" in writer.output


@pytest.mark.parametrize(
    ("exhausted", "code", "action", "uncertain"),
    [
        ("Browser research navigation limit reached",
         "browser_navigation_budget_exhausted", "open", False),
        ("Browser network request limit reached",
         "browser_network_budget_exhausted", "click", True),
        ("Browser screenshot exceeds the bounded output size",
         "browser_screenshot_too_large", "screenshot", False),
    ],
)
def test_browser_budget_diagnostics_are_typed(exhausted, code, action, uncertain):
    result = server_browser.browser_failure(ValueError(exhausted), action)
    assert result["code"] == code
    assert result["outcome_uncertain"] is uncertain
    assert exhausted not in result.values()


@pytest.mark.parametrize("limiter", ["navigation", "network"])
async def test_existing_session_refuses_exhausted_research(limiter):
    pool = server_browser.BrowserPool()
    key = ("actor", "channel", "goal")
    proxy = SimpleNamespace(exhausted=limiter == "network", close=AsyncMock())
    session = server_browser.Session(SimpleNamespace(close=AsyncMock()), proxy)
    session.navigation_budget_exhausted = limiter == "navigation"
    pool.sessions[key] = session
    pool.browser = SimpleNamespace(is_connected=lambda: True)
    with pytest.raises(ValueError, match="limit reached"):
        await pool.execute(key, server_browser.BrowserAction(action="snapshot"))
    # Exhaustion does not authorize a silent new session, but explicit close works.
    result = await pool.execute(key, server_browser.BrowserAction(action="close"))
    assert result["code"] == "session_closed"
    assert key not in pool.sessions


async def test_session_deadline_closes_without_replaying_browser_action(monkeypatch):
    monkeypatch.setattr(server_browser, "MAX_BROWSER_SESSION_SECONDS", 1)
    pool = server_browser.BrowserPool()
    key = ("actor", "channel", "goal")
    context, proxy = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    session = server_browser.Session(context, proxy)
    session.created -= 10
    pool.sessions[key] = session
    pool.browser = SimpleNamespace(is_connected=lambda: True)
    result = await pool.execute(key, server_browser.BrowserAction(action="snapshot"))
    assert result["available"] is False
    assert result["code"] == "browser_session_deadline_exceeded"
    assert key not in pool.sessions
    context.close.assert_awaited_once()
    proxy.close.assert_awaited_once()


async def test_screenshot_output_is_bounded(monkeypatch):
    monkeypatch.setattr(server_browser, "MAX_BROWSER_SCREENSHOT_BYTES", 8)
    pool = server_browser.BrowserPool()
    key = ("actor", "channel", "goal")
    screenshot = AsyncMock(return_value=b"large capture")
    page = SimpleNamespace(is_closed=lambda: False, url="https://fixture.example/",
                           screenshot=screenshot)
    session = server_browser.Session(SimpleNamespace(), SimpleNamespace(), page=page)
    pool.sessions[key] = session
    pool.browser = SimpleNamespace(is_connected=lambda: True)
    with pytest.raises(ValueError, match="bounded output size"):
        await pool.execute(key, server_browser.BrowserAction(action="screenshot"))
    screenshot.assert_awaited_once()


async def test_snapshot_exposes_no_secret_bearing_budget_values():
    page = SimpleNamespace(
        frames=[], locator=lambda _: SimpleNamespace(
            inner_text=AsyncMock(return_value="fixture page")),
        url="https://fixture.example/path?token=private", title=AsyncMock(return_value="Fixture")
    )
    proxy = SimpleNamespace(connect_attempts=7, exhausted=False)
    session = server_browser.Session(SimpleNamespace(pages=[page]), proxy, page=page)
    session.navigations = 3
    result = await server_browser.BrowserPool().snapshot(session)
    assert result["research_budget"]["navigations_used"] == 3
    assert result["research_budget"]["network_connects_used"] == 7
    assert "token" not in str(result["research_budget"])
