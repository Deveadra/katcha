from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx

from katcha.config import Settings, get_settings
from katcha.editorial.visual_schemas import EditorialRenderManifest
from katcha.rendering.blueprint_manifest import BlueprintRenderManifest
from katcha.rendering.longform_manifest import LongformRenderManifest
from katcha.rendering.manifest import ShortRenderManifest
from katcha.rendering.ranked_episode_manifest import RankedEpisodeRenderManifest
from katcha.rendering.thumbnail_manifest import ThumbnailRenderManifest
from katcha.runtime_fence import assert_mutation_authority


@dataclass(frozen=True, slots=True)
class RenderResult:
    output_key: str
    duration_seconds: float
    metadata: dict[str, Any]


class RendererRequestError(RuntimeError):
    pass


class EditorialRenderRejected(RendererRequestError):
    """Renderer confirmed validation/config rejection before any compute was dispatched."""


def _raise_for_renderer_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    detail = response.text.strip()
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        detail = str(payload["error"]).strip()
    if not detail:
        detail = response.reason_phrase or "renderer request failed"
    raise RendererRequestError(f"renderer HTTP {response.status_code}: {detail}")


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
        | EditorialRenderManifest
    ),
    *,
    settings: Settings,
) -> RenderResult:
    assert_mutation_authority(
        "render.dispatch",
        settings=settings,
    )
    url = f"{settings.renderer_url.rstrip('/')}/render"
    with httpx.Client(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
        response = client.post(url, json=manifest.model_dump(mode="json"))
        if isinstance(manifest, EditorialRenderManifest) and response.status_code == 400:
            raise EditorialRenderRejected(
                "Editorial renderer rejected this request before dispatch"
            )
        _raise_for_renderer_error(response)
        payload = response.json()
    output_key = payload.get("output_key")
    if not isinstance(output_key, str) or not output_key:
        raise RuntimeError("renderer response did not contain output_key")
    if isinstance(manifest, EditorialRenderManifest):
        duration = payload.get("duration_seconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise RendererRequestError("Editorial renderer response omitted measured duration")
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
    assert_mutation_authority(
        "render.thumbnail_dispatch",
        settings=resolved,
    )
    url = f"{resolved.renderer_url.rstrip('/')}/thumbnail"
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        response = client.post(url, json=manifest.model_dump(mode="json"))
        _raise_for_renderer_error(response)
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


def render_editorial(
    manifest: EditorialRenderManifest,
    *,
    settings: Settings | None = None,
) -> RenderResult:
    result = _render(manifest, settings=settings or get_settings())
    if (
        not math.isfinite(result.duration_seconds)
        or result.output_key != manifest.output_key
        or abs(result.duration_seconds - manifest.output_duration_seconds) > 1 / manifest.fps
        or result.metadata.get("verified") is not True
        or result.metadata.get("composition") != "Editorial"
    ):
        raise RendererRequestError("Editorial renderer returned an unverified or mismatched output")
    return result


def recover_editorial_output(
    manifest: EditorialRenderManifest, *, settings: Settings | None = None
) -> RenderResult | None:
    """Read-only reconciliation. Never dispatch replacement compute on uncertain completion."""
    resolved = settings or get_settings()
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        response = client.post(
            f"{resolved.renderer_url.rstrip('/')}/editorial-output",
            json=manifest.model_dump(mode="json"),
        )
        if response.status_code == 404:
            return None
        _raise_for_renderer_error(response)
        payload = response.json()
    if (
        not math.isfinite(float(payload.get("duration_seconds", 0)))
        or payload.get("output_key") != manifest.output_key
        or payload.get("metadata", {}).get("verified") is not True
        or payload.get("metadata", {}).get("composition") != "Editorial"
        or abs(float(payload.get("duration_seconds", 0)) - manifest.output_duration_seconds)
        > 1 / manifest.fps
    ):
        raise RendererRequestError("Recovered editorial output failed verification")
    return RenderResult(
        payload["output_key"], float(payload["duration_seconds"]), payload["metadata"]
    )
