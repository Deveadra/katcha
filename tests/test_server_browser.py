"""Actual Chromium against a local TLS fixture; no live provider/media/rights claim."""
from __future__ import annotations

import asyncio
import os
import ssl
import subprocess

import pytest

from katcha.services import server_browser as browser

pytestmark = pytest.mark.skipif(
    os.environ.get("KATCHA_TEST_REAL_BROWSER") != "1", reason="requires installed Chromium"
)


@pytest.fixture
async def real_pool(monkeypatch, tmp_path):
    from playwright.async_api import Browser

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=fixture.test"], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    requests = []

    async def serve(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            requests.append(header.decode())
            path = header.split(b" ")[1]
            if path.startswith(b"/next"):
                body = b"<html><body><h1>Second page</h1></body></html>"
            else:
                body = b'''<html><body><h1>Fixture login</h1>
                <input aria-label="Name"><button onclick="document.cookie='session=ok; Secure';
                document.querySelector('h1').textContent=
                'Hello '+document.querySelector('input').value">
                Sign in</button><a href="/next">Next</a>
                <a href="/next" target="_blank">New tab</a>
                <select aria-label="Choice"><option value="a">A</option><option value="b">B</option>
                </select><div id="state"></div><script>
                document.querySelector('#state').textContent=document.cookie;
                fetch('https://127.0.0.1/private').catch(()=>{});
                </script></body></html>'''
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=tls)
    port = server.sockets[0].getsockname()[1]
    original_addresses = browser.public_addresses
    original_context = Browser.new_context

    def addresses(url):
        if url.startswith("https://fixture.test"):
            return "fixture.test", ["127.0.0.1"]
        return original_addresses(url)

    async def connect(self, address):
        assert address == "127.0.0.1"
        return await asyncio.open_connection(address, port)

    async def context(self, **kwargs):
        return await original_context(self, **kwargs, ignore_https_errors=True)

    monkeypatch.setattr(browser, "public_addresses", addresses)
    monkeypatch.setattr(browser.PublicProxy, "connect", connect)
    monkeypatch.setattr(Browser, "new_context", context)
    pool = browser.BrowserPool()
    try:
        yield pool, requests
    finally:
        await pool.stop()
        server.close()
        await server.wait_closed()


def control(snapshot, name):
    return next(e["ref"] for e in snapshot["elements"] if e["name"] == name)


async def test_real_javascript_forms_cookies_isolation_tabs_and_screenshot(real_pool):
    pool, requests = real_pool
    scope = ("actor", "channel", "goal")

    snapshot_id = None

    async def act(action, **kwargs):
        nonlocal snapshot_id
        if action in {"mouse_click", "drag", "type", "screenshot"} or (
            action == "press" and not kwargs.get("ref")
        ):
            kwargs["snapshot_id"] = snapshot_id
        result = await pool.execute(scope, browser.BrowserAction(action=action, **kwargs))
        if "snapshot_id" in result:
            snapshot_id = result["snapshot_id"]
        return result

    first = await act("open", url="https://fixture.test/")
    assert "Fixture login" in first["text"]
    await act("hover", ref=control(first, "Name"))
    page = pool.sessions[scope].page
    box = await page.locator("input").bounding_box()
    await act("mouse_click", x=int(box["x"] + 5), y=int(box["y"] + 5))
    await act("type", text="Typed through the keyboard")
    assert await page.locator("input").input_value() == "Typed through the keyboard"
    await act("press", key="ControlOrMeta+A")
    await act("type", text="Ada")
    assert await page.locator("input").input_value() == "Ada"
    refreshed = await act("snapshot")
    filled = await act("fill", ref=control(refreshed, "Name"), text="Ada")
    signed = await act("click", ref=control(filled, "Sign in"))
    assert "Hello Ada" in signed["text"]
    choices = next(e for e in signed["elements"] if e["name"] == "Choice")
    option = next(o["value"] for o in choices["options"] if o["label"] == "B")
    selected = await act("select", ref=choices["ref"], text=option)
    assert selected["available"]
    reopened = await act("open", url="https://fixture.test/")
    assert "session=ok" in reopened["text"]
    isolated = await pool.execute(("other", "channel", "goal"),
                                 browser.BrowserAction(action="open", url="https://fixture.test/"))
    assert "session=ok" not in isolated["text"]
    image = await act("screenshot")
    assert image["image"].startswith("iVBOR")
    next_page = await act("click", ref=control(reopened, "Next"))
    assert "Second page" in next_page["text"]
    back = await act("back")
    assert "Fixture login" in back["text"]
    await act("click", ref=control(back, "New tab"))
    tabs = await act("snapshot")
    assert len(tabs["tabs"]) == 2
    switched = await act("switch_tab", tab=1)
    assert "Second page" in switched["text"]
    closed_tab = await act("close_tab", tab=1)
    assert len(closed_tab["tabs"]) == 1
    assert not any("/private" in r for r in requests)
    await act("close")
    expired = await act("snapshot")
    assert expired["code"] == "session_expired"


async def test_real_private_navigation_rejected_and_stale_refs_do_not_act(real_pool):
    pool, _ = real_pool
    scope = ("actor", "channel", "goal")
    first = await pool.execute(scope, browser.BrowserAction(action="open",
                                                          url="https://fixture.test/"))
    stale = control(first, "Sign in")
    stale_snapshot = first["snapshot_id"]
    await pool.execute(scope, browser.BrowserAction(action="snapshot"))
    with pytest.raises(ValueError, match="Refresh"):
        await pool.execute(scope, browser.BrowserAction(action="click", ref=stale))
    with pytest.raises(ValueError, match="Refresh"):
        await pool.execute(scope, browser.BrowserAction(action="mouse_click", x=20, y=20,
                                                       snapshot_id=stale_snapshot))
    with pytest.raises(ValueError, match="Refresh"):
        await pool.execute(scope, browser.BrowserAction(action="type", text="wrong page"))
    with pytest.raises(ValueError, match="non-public"):
        await pool.execute(scope, browser.BrowserAction(action="open", url="https://127.0.0.1/"))


async def test_real_chromium_cannot_exceed_navigation_budget(monkeypatch, real_pool):
    pool, _ = real_pool
    monkeypatch.setattr(browser, "MAX_BROWSER_NAVIGATIONS", 1)
    key = ("actor", "channel", "bounded-goal")
    first = await pool.execute(
        key, browser.BrowserAction(action="open", url="https://fixture.test/")
    )
    assert first["research_budget"]["navigations_used"] == 1
    assert first["research_budget"]["navigations_remaining"] == 0
    with pytest.raises(ValueError, match="navigation limit reached"):
        await pool.execute(
            key, browser.BrowserAction(action="open", url="https://fixture.test/next")
        )
    assert pool.sessions[key].navigation_budget_exhausted is True


async def test_real_acquisition_browser_exports_only_its_own_session(real_pool, tmp_path):
    """Actual Chromium sign-in fixture feeds the acquisition retry cookie format."""
    import http.cookiejar
    from datetime import UTC, datetime, timedelta

    from katcha.acquisition.human_browser import HumanBrowser

    pool, _ = real_pool
    human = HumanBrowser.__new__(HumanBrowser)
    human.pool = pool
    human.loop = asyncio.get_running_loop()
    permission = {
        "provider": "fixture", "operations": ["download"], "allowed_hosts": ["fixture.test"],
        "evidence_scope": "local synthetic browser test", "evidence_refs": ["fixture:authorized"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }

    async def act(action):
        return await asyncio.to_thread(human.execute, "channel", "original-job", action,
                                       "https://fixture.test/", permission)

    opened = await act({"action": "open"})
    assert opened["available"]
    filled = await act({"action": "fill", "ref": control(opened, "Name"), "text": "Operator"})
    signed = await act({"action": "click", "ref": control(filled, "Sign in")})
    assert "Hello Operator" in signed["text"]
    destination = tmp_path / "human-session.cookies"
    agent = await asyncio.to_thread(human.export_cookies, "channel", "original-job",
                                    permission, destination)
    assert agent and destination.stat().st_mode & 0o077 == 0
    jar = http.cookiejar.MozillaCookieJar(str(destination))
    jar.load(ignore_discard=True)
    assert [(cookie.name, cookie.value) for cookie in jar] == [("session", "ok")]
    with pytest.raises(ValueError, match="expired"):
        await asyncio.to_thread(human.export_cookies, "channel", "different-job",
                                permission, tmp_path / "other.cookies")
    assert not (tmp_path / "other.cookies").exists()
    await asyncio.to_thread(human.close, "channel", "original-job")
    with pytest.raises(ValueError, match="expired"):
        await asyncio.to_thread(human.export_cookies, "channel", "original-job",
                                permission, tmp_path / "closed.cookies")
