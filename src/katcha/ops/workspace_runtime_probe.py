"""Offline acceptance for built workspace images; never run a worker or provider."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


class RuntimeProbeError(RuntimeError):
    pass


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        path = (
            values.get("src") if tag == "script"
            else values.get("href") if tag == "link" else None
        )
        if path and path.startswith("/"):
            self.paths.add(urlsplit(path).path)


def probe_media() -> None:
    import av
    from PIL import Image

    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise RuntimeProbeError(f"missing media executable: {executable}")
    with tempfile.TemporaryDirectory(prefix="katcha-runtime-fixture-") as directory:
        path = Path(directory) / "fixture.mp4"
        with av.open(str(path), "w") as output:
            stream = output.add_stream("mpeg4", rate=1)
            stream.width = stream.height = 64
            stream.pix_fmt = "yuv420p"
            for color in ("red", "blue"):
                frame = av.VideoFrame.from_image(Image.new("RGB", (64, 64), color))
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
        with av.open(str(path), "r", metadata_errors="ignore") as source:
            if len(list(source.decode(video=0))) != 2:
                raise RuntimeProbeError("fixture video decoding failed")
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, check=True,
        )
        streams = json.loads(result.stdout)["streams"]
        if not streams or streams[0]["width"] != 64:
            raise RuntimeProbeError("fixture video probing failed")


def probe_workspace_assets() -> int:
    from fastapi.testclient import TestClient

    from katcha.api.main import app

    assets: set[str] = set()
    # Route-only requests deliberately skip lifespan: it processes real handoffs.
    client = TestClient(app)
    try:
        for route in (
            "/", "/home", "/explorer", "/ingestion", "/clips", "/channels",
            "/content", "/editing", "/studio", "/ai", "/operations", "/settings",
        ):
            page = client.get(route)
            if page.status_code != 200 or "text/html" not in page.headers.get("content-type", ""):
                raise RuntimeProbeError(f"workspace route unavailable: {route}")
            parser = _Assets()
            parser.feed(page.text)
            assets.update(parser.paths)
        for path in sorted(assets):
            response = client.get(path)
            if response.status_code != 200 or not response.content:
                raise RuntimeProbeError(f"workspace asset unavailable: {path}")
            content_type = response.headers.get("content-type", "")
            if path.endswith(".css") and "text/css" not in content_type:
                raise RuntimeProbeError(f"workspace stylesheet invalid: {path}")
            if path.endswith(".js") and "javascript" not in content_type:
                raise RuntimeProbeError(f"workspace script invalid: {path}")
    finally:
        client.close()
    return len(assets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["control", "production"])
    args = parser.parse_args()
    probe_media()
    result: dict[str, object] = {"role": args.role, "fixture_media": "passed"}
    if args.role == "control":
        result["workspace_assets"] = probe_workspace_assets()
    else:
        # Import the real shared worker factory, including every activity module.
        from katcha.orchestration.longform_worker import create_longform_worker
        if not callable(create_longform_worker):
            raise RuntimeProbeError("shared Editorial worker factory unavailable")
        result["editorial_imports"] = "passed"
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
