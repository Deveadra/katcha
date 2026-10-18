"""Isolated, ephemeral browser service. Never import application credentials here."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from katcha.editorial.retrieval import public_addresses


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["open", "snapshot", "click", "fill", "press", "select", "scroll", "wait",
                    "back", "tabs", "switch_tab", "close_tab", "close", "screenshot"]
    url: str | None = Field(default=None, max_length=2000)
    ref: str | None = Field(default=None, pattern=r"^e[0-9]+$", max_length=12)
    text: str = Field(default="", max_length=10000)
    key: str = Field(default="Enter", max_length=40)
    tab: int = Field(default=0, ge=0, le=7)
    scroll_y: int = Field(default=600, ge=-4000, le=4000)
    wait_ms: int = Field(default=500, ge=0, le=5000)

    @model_validator(mode="after")
    def required_fields(self):
        if self.action == "open" and not self.url:
            raise ValueError("Enter a public HTTPS address")
        if self.action in {"click", "fill", "press", "select"} and not self.ref:
            raise ValueError("Choose an element from the current page")
        return self


class BrowserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: tuple[str, str, str]
    arguments: BrowserAction


class PublicProxy:
    """CONNECT only; validate DNS then connect to the literal address to prevent rebinding."""
    def __init__(self):
        self.server = None
        self.writers = set()
        self.tasks = set()
        self.closed = False
        self.connections = asyncio.Semaphore(64)

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=8192)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.closed = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def connect(self, address):
        return await asyncio.open_connection(address, 443)

    async def handle(self, reader, writer):
        upstream = None
        self.writers.add(writer)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with self.connections:
                if self.closed:
                    return
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                method, target, _ = headers.split(b"\r\n", 1)[0].decode("ascii").split()
                if method != "CONNECT" or not target.endswith(":443"):
                    raise ValueError("Only HTTPS is supported")
                host, addresses = await asyncio.to_thread(public_addresses, "https://" + target)
                remote, upstream = await asyncio.wait_for(
                    self.connect(addresses[0]), 10)
                self.writers.add(upstream)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                async def pump(source, destination):
                    while data := await source.read(65536):
                        destination.write(data)
                        await destination.drain()

                tasks = [asyncio.create_task(pump(reader, upstream)),
                         asyncio.create_task(pump(remote, writer))]
                try:
                    await asyncio.wait(tasks, timeout=120, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            with suppress(Exception):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        finally:
            self.tasks.discard(task)
            for connection in (writer, upstream):
                if connection:
                    self.writers.discard(connection)
                    connection.close()


@dataclass
class Session:
    context: object
    proxy: PublicProxy
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    page: object = None
    refs: dict = field(default_factory=dict)
    next_ref: int = 0


class BrowserPool:
    def __init__(self):
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.playwright = None
        self.browser = None

    async def close_session(self, key):
        session = self.sessions.pop(key, None)
        if session:
            try:
                with suppress(Exception):
                    await session.context.close()
            finally:
                await session.proxy.close()

    async def reap(self):
        while True:
            await asyncio.sleep(30)
            async with self.lock:
                for key, session in list(self.sessions.items()):
                    if not session.lock.locked() and time.monotonic() - session.touched > 900:
                        await self.close_session(key)

    async def stop(self):
        for key in list(self.sessions):
            await self.close_session(key)
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def ensure_browser(self):
        if not self.browser or not self.browser.is_connected():
            for key in list(self.sessions):
                await self.close_session(key)
            from playwright.async_api import async_playwright
            if self.playwright:
                await self.playwright.stop()
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                      "--proxy-bypass-list=<-loopback>"],
            )

    async def session(self, key, create):
        async with self.lock:
            session = self.sessions.get(key)
            if session and not self.browser.is_connected():
                await self.close_session(key)
                session = None
            if session or not create:
                return session
            if len(self.sessions) >= 8:
                raise ValueError("All browser sessions are busy. Close a session and try again.")
            await self.ensure_browser()
            proxy = PublicProxy()
            port = await proxy.start()
            context = None
            try:
                context = await self.browser.new_context(
                    proxy={"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"},
                    service_workers="block", accept_downloads=False,
                    viewport={"width": 1280, "height": 800},
                )
                context.set_default_timeout(10000)
                context.set_default_navigation_timeout(20000)

                async def route(request_route):
                    if urlsplit(request_route.request.url).scheme != "https":
                        await request_route.abort()
                    else:
                        await request_route.continue_()
                await context.route("**/*", route)
                # WebSocket traffic uses the same pinned CONNECT proxy.
                session = Session(context, proxy)
                session.page = await context.new_page()
                self.sessions[key] = session
                return session
            except BaseException:
                if context:
                    with suppress(Exception):
                        await context.close()
                await proxy.close()
                raise

    async def snapshot(self, session):
        page = session.page
        for handle in session.refs.values():
            with suppress(Exception):
                await handle.dispose()
        session.refs = {}
        elements = []
        # Frame-local locators remain bound to the actual DOM nodes until next snapshot.
        for frame in page.frames[:16]:
            locator = frame.locator(
                "a[href],button,input,textarea,select,[role=button],[contenteditable=true]"
            )
            for item in (await locator.all())[:500]:
                if len(elements) >= 150:
                    break
                if not await item.is_visible():
                    continue
                description = await item.evaluate("""e => ({tag:e.tagName.toLowerCase(),
                    role:e.getAttribute('role')||'',type:e.getAttribute('type')||'',
                    name:(e.getAttribute('aria-label')||e.labels?.[0]?.innerText||
                          e.innerText||e.getAttribute('placeholder')||e.getAttribute('name')||'')
                          .slice(0,200)})""")
                ref = f"e{session.next_ref}"
                session.next_ref += 1
                session.refs[ref] = await item.element_handle()
                elements.append({"ref": ref, **description})
        text = await page.locator("body").inner_text(timeout=5000)
        return {
            "available": True, "session_id": session.session_id, "url": page.url,
            "title": await page.title(), "text": text[:18000], "elements": elements,
            "tabs": [{"index": i, "url": p.url} for i, p in enumerate(session.context.pages)],
            "observed_at": datetime.now(UTC).isoformat(),
            "active_tab": session.context.pages.index(page),
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "untrusted_content": True, "scene_verified": False,
        }

    async def execute(self, scope, action):
        session = await self.session(tuple(scope), action.action == "open")
        if not session:
            return {"available": False, "code": "session_expired",
                    "message": "Open a page to start a new browser session. Previous sign-in state "
                               "expired. Check submitted work before retrying."}
        async with session.lock:
            if self.sessions.get(tuple(scope)) is not session:
                return {"available": False, "code": "session_expired",
                        "message": "This browser session closed. Open a page to start again."}
            session.touched = time.monotonic()
            if action.action == "close":
                async with self.lock:
                    await self.close_session(tuple(scope))
                return {"available": False, "code": "session_closed", "message": "Browser closed."}
            page = session.page
            if page.is_closed():
                pages = session.context.pages
                session.page = page = pages[-1] if pages else await session.context.new_page()
            if action.action == "open":
                await asyncio.to_thread(public_addresses, action.url)
                await page.goto(action.url, wait_until="domcontentloaded")
            elif action.action in {"click", "fill", "press", "select"}:
                element = session.refs.get(action.ref)
                if not element:
                    raise ValueError("Refresh the page view and choose the control again.")
                # Keep handles for disposal by the next snapshot.
                if action.action == "click":
                    await element.click()
                elif action.action == "fill":
                    await element.fill(action.text)
                elif action.action == "select":
                    await element.select_option(action.text)
                else:
                    await element.press(action.key)
                if len(session.context.pages) > 1 and session.context.pages[-1] != page:
                    session.page = session.context.pages[-1]
            elif action.action == "scroll":
                await page.mouse.wheel(0, action.scroll_y)
            elif action.action == "wait":
                await page.wait_for_timeout(action.wait_ms)
            elif action.action == "back":
                await page.go_back(wait_until="domcontentloaded")
            elif action.action in {"switch_tab", "close_tab"}:
                pages = session.context.pages
                if action.tab >= len(pages):
                    raise ValueError("That tab has closed. Refresh the page view.")
                if action.action == "close_tab":
                    await pages[action.tab].close()
                    pages = session.context.pages
                    session.page = pages[-1] if pages else await session.context.new_page()
                else:
                    session.page = pages[action.tab]
            elif action.action == "screenshot":
                return {"available": True, "image": base64.b64encode(
                    await page.screenshot(type="png")).decode()}
            if len(session.context.pages) > 8:
                for extra in session.context.pages[8:]:
                    await extra.close()
            return await self.snapshot(session)


pool = BrowserPool()


@asynccontextmanager
async def lifespan(app):
    reaper = asyncio.create_task(pool.reap())
    try:
        yield
    finally:
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
        await pool.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/execute")
async def execute(request: BrowserRequest, authorization: str = Header(default="")):
    token = os.environ.get("KATCHA_BROWSER_SERVICE_TOKEN", "")
    if len(token) < 32 or not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Browser service authentication required")
    try:
        return await asyncio.wait_for(pool.execute(request.scope, request.arguments), 45)
    except Exception:
        # URLs, form values and Playwright exceptions may contain passwords; never log/return them.
        return {"available": False, "code": "browser_action_failed", "outcome_uncertain": True,
                "message": "The browser action could not finish. Refresh the page view to inspect "
                           "the result before repeating an action. Otherwise reopen the page."}


@app.get("/health")
async def health():
    if len(os.environ.get("KATCHA_BROWSER_SERVICE_TOKEN", "")) < 32:
        raise HTTPException(503, "Configure a browser service token with at least 32 characters")
    try:
        async with pool.lock:
            await asyncio.wait_for(pool.ensure_browser(), 30)
    except Exception:
        raise HTTPException(503, "Chromium could not start; check runtime dependencies") from None
    return {"ready": True, "sessions": len(pool.sessions)}
