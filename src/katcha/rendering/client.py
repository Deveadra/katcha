from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from katcha.config import Settings, get_settings
from katcha.rendering.blueprint_manifest import BlueprintRenderManifest
from katcha.rendering.longform_manifest import LongformRenderManifest
from katcha.rendering.manifest import ShortRenderManifest
from katcha.rendering.ranked_episode_manifest import RankedEpisodeRenderManifest
from katcha.rendering.thumbnail_manifest import ThumbnailRenderManifest


@dataclass(frozen=True, slots=True)
class RenderResult:
    output_key: str
    duration_seconds: float
    metadata: dict[str, Any]



@dataclass(frozen=True, slots=True)
class ThumbnailRenderResult:
    output_key: str
    width: int
    height: int
    metadata: dict[str, Any]


def _render(
    manifest: (
        ShortRenderManifest
        | LongformRenderManifest
        | RankedEpisodeRenderManifest
        | BlueprintRenderManifest
    ),
    *,
    settings: Settings,
) -> RenderResult:
    url = f"{settings.renderer_url.rstrip('/')}/render"
    with httpx.Client(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
        response = client.post(url, json=manifest.model_dump(mode="json"))
        response.raise_for_status()
        payload = response.json()
    output_key = payload.get("output_key")
    if not isinstance(output_key, str) or not output_key:
        raise RuntimeError("renderer response did not contain output_key")
    return RenderResult(
        output_key=output_key,
        duration_seconds=float(payload.get("duration_seconds") or manifest.output_duration_seconds),
        metadata=dict(payload.get("metadata") or {}),
    )


def render_short(
    manifest: ShortRenderManifest,
    *,
    settings: Settings | None = None,
) -> RenderResult:
    return _render(manifest, settings=settings or get_settings())


def render_ranked_episode(
    manifest: RankedEpisodeRenderManifest,
    *,
    settings: Settings | None = None,
) -> RenderResult:
    return _render(manifest, settings=settings or get_settings())


def render_blueprint(
    manifest: BlueprintRenderManifest,
    *,
    settings: Settings | None = None,
) -> RenderResult:
    return _render(manifest, settings=settings or get_settings())


def render_longform(
    manifest: LongformRenderManifest,
    *,
    settings: Settings | None = None,
) -> RenderResult:
    return _render(manifest, settings=settings or get_settings())


def render_thumbnail(
    manifest: ThumbnailRenderManifest,
    *,
    settings: Settings | None = None,
) -> ThumbnailRenderResult:
    resolved = settings or get_settings()
    url = f"{resolved.renderer_url.rstrip('/')}/thumbnail"
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        response = client.post(url, json=manifest.model_dump(mode="json"))
        response.raise_for_status()
        payload = response.json()
    output_key = payload.get("output_key")
    if not isinstance(output_key, str) or not output_key:
        raise RuntimeError("renderer response did not contain thumbnail output_key")
    return ThumbnailRenderResult(
        output_key=output_key,
        width=int(payload.get("width") or manifest.width),
        height=int(payload.get("height") or manifest.height),
        metadata=dict(payload.get("metadata") or {}),
    )
