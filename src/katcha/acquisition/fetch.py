"""Approved public HTTPS acquisition, with pinned DNS and bounded provider retries."""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from katcha.editorial.media_intake.contracts import ProviderPermissionV1
from katcha.editorial.media_intake.transport import PublicDestinationTransport


class FetchFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def check_url(url, permission):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.port not in {None, 443}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not permission.allows_host(parsed.hostname)
        or not permission.allows("download")
    ):
        raise FetchFailure("access_denied")


def fetch_file(
    url, permission, path: Path, max_bytes, checkpoint, progress, *, client_factory=None
):
    """Resume only a strongly ETag-bound representation; never silently start it again."""
    permission = ProviderPermissionV1.model_validate(permission)
    check_url(url, permission)
    offset = path.stat().st_size if path.exists() else 0
    etag = checkpoint.get("etag")
    if offset and (not etag or etag.startswith("W/")):
        raise FetchFailure("outcome_unknown")
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers.update({"Range": f"bytes={offset}-", "If-Match": etag})
    factory = client_factory or (
        lambda: httpx.Client(
            transport=PublicDestinationTransport(permission),
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
            trust_env=False,
        )
    )
    with factory() as client:
        for redirect in range(4):
            check_url(url, permission)
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect == 3 or not response.headers.get("location"):
                        raise FetchFailure("access_denied")
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code in {401, 403}:
                    raise FetchFailure("authentication_required")
                if response.status_code == 429:
                    raise FetchFailure("rate_limited")
                if response.status_code not in {200, 206}:
                    raise FetchFailure("access_denied")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise FetchFailure("invalid_media")
                if offset:
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", "")
                    )
                    if (
                        response.status_code != 206
                        or not match
                        or int(match[1]) != offset
                        or int(match[2]) + 1 != int(match[3])
                        or response.headers.get("etag") != etag
                        or int(match[3]) != checkpoint.get("total")
                    ):
                        raise FetchFailure("outcome_unknown")
                    total = int(match[3])
                else:
                    if response.status_code != 200:
                        raise FetchFailure("invalid_media")
                    total = (
                        int(response.headers["content-length"])
                        if response.headers.get("content-length")
                        else None
                    )
                    checkpoint.update(etag=response.headers.get("etag"), total=total)
                if total is not None and not 0 < total <= max_bytes:
                    raise FetchFailure("size_limit")
                if response.headers.get("content-type", "").split(";")[0] in {
                    "text/html",
                    "application/json",
                }:
                    raise FetchFailure("authentication_required")
                progress(offset)  # durably save ETag/representation before writing any bytes
                with path.open("ab" if offset else "wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if not permission.allows("download"):
                            raise FetchFailure("access_denied")
                        if offset + len(chunk) > max_bytes:
                            raise FetchFailure("size_limit")
                        if shutil.disk_usage(path.parent).free < len(chunk) + 256 * 1024**2:
                            raise FetchFailure("size_limit")
                        output.write(chunk)
                        output.flush()
                        offset += len(chunk)
                        progress(offset)
                    import os

                    os.fsync(output.fileno())
                if offset == 0 or total is not None and offset != total:
                    raise FetchFailure("transport_error")
                return path
    raise FetchFailure("access_denied")


class _ResponseStream(io.RawIOBase):
    def __init__(self, response, client):
        self.response, self.client = response, client
        self.iterator, self.buffer = response.iter_bytes(64 * 1024), b""

    def readable(self):
        return True

    def readinto(self, target):
        if not self.buffer:
            self.buffer = next(self.iterator, b"")
        count = min(len(target), len(self.buffer))
        target[:count], self.buffer = self.buffer[:count], self.buffer[count:]
        return count

    def close(self):
        self.response.close()
        self.client.close()
        super().close()


def fetch_extracted(url, permission, directory, max_bytes, progress, *, cookie_file=None):
    """Use yt-dlp's extractors with *all* HTTP through the approved pinned transport.

    Only HTTPS native downloads and local FFmpeg merging are selected. No external
    downloader, proxy environment, provider credential, or arbitrary extractor argument
    is accepted from a job. Cookies are an optional host-local operator configuration.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.common import Request, Response
    from yt_dlp.networking.exceptions import HTTPError
    from yt_dlp.utils import DownloadError

    permission = ProviderPermissionV1.model_validate(permission)
    check_url(url, permission)

    class BoundedResponse(Response):
        def read(self, amt=None):
            # Extractors read metadata without a length. Media downloaders use
            # bounded reads; a source must not turn metadata into an unbounded allocation.
            if amt is None or amt < 0:
                data = super().read(16 * 1024**2 + 1)
                if len(data) > 16 * 1024**2:
                    raise FetchFailure("size_limit")
                return data
            return super().read(amt)

    class ApprovedDownloader(YoutubeDL):
        def urlopen(self, request):
            request = Request(request) if isinstance(request, str) else request
            target = request.url
            # A fresh client per response avoids extractor/download stream deadlocks.
            client = httpx.Client(
                transport=PublicDestinationTransport(permission),
                cookies=self.cookiejar,
                timeout=httpx.Timeout(30, connect=5),
                trust_env=False,
            )
            try:
                for redirect in range(4):
                    progress(0)
                    check_url(target, permission)
                    built = client.build_request(
                        request.method,
                        target,
                        headers={
                            k: v
                            for k, v in request.headers.items()
                            if k.casefold() not in {"host", "accept-encoding"}
                        },
                        content=request.data,
                    )
                    built.headers["Accept-Encoding"] = "identity"
                    response = client.send(built, stream=True, follow_redirects=False)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        response.close()
                        if redirect == 3 or not location:
                            raise FetchFailure("access_denied")
                        new_target = urljoin(target, location)
                        if urlsplit(new_target).netloc != urlsplit(target).netloc:
                            # Do not forward provider authorization across origins.
                            request.headers.pop("Authorization", None)
                            request.headers.pop("Cookie", None)
                        target = new_target
                        continue
                    if response.status_code in {401, 403, 429}:
                        response.close()
                        raise FetchFailure(
                            "rate_limited"
                            if response.status_code == 429
                            else "authentication_required"
                        )
                    adapted = BoundedResponse(
                        io.BufferedReader(_ResponseStream(response, client)),
                        str(response.url),
                        dict(response.headers),
                        response.status_code,
                    )
                    if response.status_code >= 400:
                        raise HTTPError(adapted)
                    return adapted
                raise FetchFailure("access_denied")
            except Exception:
                client.close()
                raise

    class QuietLogger:
        def debug(self, message):
            pass

        warning = error = debug

    def hook(_):
        if not permission.allows("download"):
            raise FetchFailure("access_denied")
        size = sum(p.stat().st_size for p in directory.glob("media.*") if p.is_file())
        if size > max_bytes or shutil.disk_usage(directory).free < 256 * 1024**2:
            raise FetchFailure("size_limit")
        progress(size)

    options = dict(
        outtmpl=str(directory / "media.%(ext)s"),
        format="bestvideo[protocol=https]+bestaudio[protocol=https]/best[protocol=https]",
        merge_output_format="mp4",
        noplaylist=True,
        quiet=True,
        no_warnings=True,
        retries=0,
        fragment_retries=0,
        extractor_retries=0,
        socket_timeout=30,
        progress_hooks=[hook],
        logger=QuietLogger(),
        js_runtimes={"node": {}, "deno": {}},
        max_filesize=max_bytes,
        cachedir=False,
        proxy="",
        enable_file_urls=False,
    )
    if cookie_file:
        options["cookiefile"] = str(cookie_file)
    try:
        with ApprovedDownloader(options) as downloader:
            downloader.extract_info(url, download=True)
    except DownloadError as exc:
        text = str(exc).casefold()
        if "sign in" in text or "captcha" in text or "confirm you" in text:
            raise FetchFailure("authentication_required") from None
        if "429" in text:
            raise FetchFailure("rate_limited") from None
        raise FetchFailure("transport_error") from None
    files = [p for p in directory.glob("media.*") if p.suffix not in {".part", ".ytdl", ".temp"}]
    if len(files) != 1 or not 0 < files[0].stat().st_size <= max_bytes:
        raise FetchFailure("invalid_media")
    return files[0]
