"""Operator-only browser on the acquisition host; cookies never leave this host."""

from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import os
import threading
import time
from pathlib import Path

from katcha.editorial.media_intake.contracts import ProviderPermissionV1
from katcha.services.server_browser import (
    MAX_BROWSER_SESSION_SECONDS,
    BrowserAction,
    BrowserPool,
    browser_failure,
)


class HumanBrowser:
    """A private event loop owns Playwright; the worker exposes no inbound port."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.pool = BrowserPool()
        self.reaper = asyncio.run_coroutine_threadsafe(self.pool.reap(), self.loop)

    def _run(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(coroutine, timeout=45), self.loop
        )
        try:
            return future.result(timeout=50)
        except BaseException:
            future.cancel()
            raise

    def ready(self):
        try:
            self._run(self.pool.ensure_browser())
            return True
        except Exception:
            return False

    @staticmethod
    def scope(channel, job_id):
        return ("acquisition-human", str(channel), str(job_id))

    def execute(self, channel, job_id, arguments, fetch_url, permission):
        """Execute one explicit operator command, never an autonomous challenge attempt."""
        action_name = str(arguments.get("action", "snapshot"))
        try:
            permitted = ProviderPermissionV1.model_validate(permission)
            if not permitted.allows("download"):
                raise ValueError("Download permission expired")
            arguments = dict(arguments)
            if action_name == "open":
                # The control plane supplies the authorized source, not a form-provided URL.
                from katcha.acquisition.fetch import check_url

                check_url(fetch_url, permitted)
                arguments["url"] = fetch_url
            action = BrowserAction.model_validate(arguments)
            return self._run(self.pool.execute(self.scope(channel, job_id), action))
        except Exception as exc:
            return browser_failure(exc, action_name)

    def export_cookies(self, channel, job_id, permission, destination: Path):
        """Write only this job's approved-host cookies into an owner-only retry jar."""
        permitted = ProviderPermissionV1.model_validate(permission)
        if not permitted.allows("download"):
            raise ValueError("Download permission expired")

        async def cookies():
            session = await self.pool.session(self.scope(channel, job_id), False)
            if session is None or time.monotonic() - session.created >= MAX_BROWSER_SESSION_SECONDS:
                raise ValueError("Verification browser session expired")
            async with session.lock:
                return await session.context.cookies(), await session.page.evaluate(
                    "navigator.userAgent"
                )

        rows, user_agent = self._run(cookies())
        jar = http.cookiejar.MozillaCookieJar(str(destination))
        now = time.time()
        for row in rows:
            domain = str(row["domain"]).lower().rstrip(".")
            cookie_host = domain.lstrip(".")
            if not any(
                host.rstrip(".").lower() == cookie_host
                or (domain.startswith(".") and host.rstrip(".").lower().endswith("." + cookie_host))
                for host in permitted.allowed_hosts
            ):
                continue
            # Netscape records must never contain injected separators.
            if any(
                char in str(row.get(key, ""))
                for key in ("name", "value", "domain", "path")
                for char in ("\t", "\r", "\n")
            ):
                continue
            expiry = row.get("expires", -1)
            if expiry > 0 and expiry <= now:
                continue
            jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=row["name"],
                    value=row["value"],
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=domain.startswith("."),
                    domain_initial_dot=domain.startswith("."),
                    path=row.get("path") or "/",
                    path_specified=True,
                    secure=bool(row.get("secure")),
                    expires=int(expiry) if expiry > 0 else None,
                    discard=expiry <= 0,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": bool(row.get("httpOnly"))},
                    rfc2109=False,
                )
            )
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            jar.save(ignore_discard=True, ignore_expires=False)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return user_agent

    def close(self, channel, job_id):
        with contextlib.suppress(Exception):
            self._run(self.pool.close_session(self.scope(channel, job_id)))

    def stop(self):
        self.reaper.cancel()
        with contextlib.suppress(Exception):
            self._run(self.pool.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.loop.close()
