"""Regression coverage for workspace assets under the actual API router.

Browser fixtures serve web/ directly at /; production serves HTML through
workspace /.../assets/ mounts. Both must resolve the same root-absolute CSS/JS.
"""

from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from katcha.api.main import app


class _DocumentAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "link" and "stylesheet" in (attributes.get("rel") or "").split():
            value = attributes.get("href")
        elif tag == "script":
            value = attributes.get("src")
        else:
            return
        if value and value.startswith("/"):
            self.urls.add(urlsplit(value).path)


@pytest.mark.parametrize(
    "route",
    [
        "/home",
        "/explorer",
        "/ingestion",
        "/clips",
        "/channels",
        "/content",
        "/editing",
        "/studio",
        "/ai",
        "/operations",
        "/settings",
    ],
)
def test_workspace_styles_and_scripts_load_from_live_api_routes(route: str) -> None:
    client = TestClient(app)
    page = client.get(route, follow_redirects=True)
    assert page.status_code == 200, (route, page.status_code)
    assert "text/html" in page.headers["content-type"]

    assets = _DocumentAssets()
    assets.feed(page.text)
    assert assets.urls, f"{route} contains no linked stylesheet or script"

    for url in sorted(assets.urls):
        response = client.get(url)
        assert response.status_code == 200, (route, url, response.status_code)
        assert response.content, (route, url, "empty asset")
        if url.endswith(".css"):
            assert "text/css" in response.headers["content-type"], (route, url)
        elif url.endswith(".js"):
            assert "javascript" in response.headers["content-type"], (route, url)


def test_channel_page_styles_match_workspace_assets_and_do_not_shadow_api() -> None:
    client = TestClient(app)
    for path in (
        "styles.css",
        "channels.css",
        "channels.js",
        "pages/channel-studio-aerith.css",
    ):
        root = client.get(f"/{path}")
        scoped = client.get(f"/channels/assets/{path}")
        assert root.status_code == scoped.status_code == 200, path
        assert root.content == scoped.content, path

    # The catch-all static mount must remain below API routes and serve only web/.
    assert client.get("/v1/health/live").status_code == 200
    assert client.get("/pyproject.toml").status_code == 404
