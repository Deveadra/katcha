from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

import httpx

from katcha.config import Settings, get_settings
from katcha.editorial.visual_schemas import EditorialRenderManifest
from katcha.ops.external_compute_budget import (
    ExternalComputeBudgetClient,
    ExternalComputeBudgetError,
    ExternalComputeReservation,
)
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


def _usd_to_microusd(value: float) -> int:
    amount = Decimal(str(value))
    if amount <= 0:
        raise RendererRequestError(
            "Lambda render budget ceiling must be positive"
        )
    return int(
        (amount * Decimal("1000000")).quantize(
            Decimal("1"),
            rounding=ROUND_UP,
        )
    )


def _lambda_budget_client(settings: Settings) -> ExternalComputeBudgetClient:
    if not settings.external_compute_enabled:
        raise RendererRequestError(
            "Lambda rendering is blocked because external compute is disabled"
        )
    coordinator = str(
        settings.external_compute_coordinator_url or ""
    ).strip()
    token = (
        settings.external_compute_token.get_secret_value().strip()
        if settings.external_compute_token is not None
        else ""
    )
    try:
        return ExternalComputeBudgetClient(
            coordinator_url=coordinator,
            token=token,
        )
    except ExternalComputeBudgetError as exc:
        raise RendererRequestError(
            f"Lambda rendering budget gate is unavailable: {exc}"
        ) from exc


def _reserve_lambda_render_budget(
    manifest: (
        ShortRenderManifest
        | LongformRenderManifest
        | RankedEpisodeRenderManifest
        | BlueprintRenderManifest
    ),
    settings: Settings,
) -> tuple[ExternalComputeBudgetClient, ExternalComputeReservation] | None:
    if settings.render_backend != "lambda":
        return None
    client = _lambda_budget_client(settings)
    estimated = _usd_to_microusd(
        settings.remotion_lambda_max_render_cost_usd
    )
    try:
        reservation = client.reserve(
            job_key=f"render:{manifest.output_key}:{uuid.uuid4().hex}",
            provider="aws-lambda",
            operation="remotion-video-render",
            retry_group=f"render:{manifest.output_key}",
            attempt=None,
            estimated_cost_microusd=estimated,
            ttl_seconds=settings.remotion_lambda_budget_ttl_seconds,
            metadata={
                "manifest_version": str(manifest.version),
                "output_key": manifest.output_key,
                "duration_seconds": float(manifest.output_duration_seconds),
            },
        )
    except ExternalComputeBudgetError as exc:
        raise RendererRequestError(
            f"Lambda render budget reservation denied: {exc}"
        ) from exc
    if reservation.status != "reserved":
        raise RendererRequestError(
            "Lambda render budget coordinator did not return an active reservation"
        )
    return client, reservation


def _release_lambda_budget(
    budget: tuple[ExternalComputeBudgetClient, ExternalComputeReservation] | None,
    *,
    reason: str,
) -> None:
    if budget is None:
        return
    client, reservation = budget
    try:
        client.release(reservation.id, reason=reason)
    except ExternalComputeBudgetError as exc:
        raise RendererRequestError(
            f"Lambda render budget release failed: {exc}"
        ) from exc


def _settle_lambda_budget(
    budget: tuple[ExternalComputeBudgetClient, ExternalComputeReservation] | None,
    *,
    reason: str,
) -> None:
    if budget is None:
        return
    client, reservation = budget
    try:
        client.settle(
            reservation.id,
            actual_cost_microusd=reservation.estimated_cost_microusd,
            metadata={"settlement_reason": reason},
        )
    except ExternalComputeBudgetError as exc:
        raise RendererRequestError(
            f"Lambda render budget settlement failed: {exc}"
        ) from exc


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
    budget = None
    if not isinstance(manifest, EditorialRenderManifest):
        budget = _reserve_lambda_render_budget(manifest, settings)

    url = f"{settings.renderer_url.rstrip('/')}/render"
    try:
        with httpx.Client(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
            response = client.post(url, json=manifest.model_dump(mode="json"))
    except httpx.HTTPError as exc:
        _settle_lambda_budget(
            budget,
            reason="renderer transport outcome uncertain",
        )
        raise RendererRequestError(
            f"renderer request failed before a response was received: {exc}"
        ) from exc

    if isinstance(manifest, EditorialRenderManifest) and response.status_code == 400:
        raise EditorialRenderRejected(
            "Editorial renderer rejected this request before dispatch"
        )

    if not response.is_success:
        if 400 <= response.status_code < 500:
            _release_lambda_budget(
                budget,
                reason=f"renderer rejected request with HTTP {response.status_code}",
            )
        else:
            _settle_lambda_budget(
                budget,
                reason=f"renderer returned HTTP {response.status_code}",
            )
        _raise_for_renderer_error(response)

    payload = response.json()
    metadata = dict(payload.get("metadata") or {})
    if budget is not None:
        if metadata.get("reused") is True:
            _release_lambda_budget(
                budget,
                reason="renderer reused an existing verified output",
            )
        elif metadata.get("renderer") == "remotion-lambda":
            _settle_lambda_budget(
                budget,
                reason="Remotion Lambda render completed",
            )
        else:
            _settle_lambda_budget(
                budget,
                reason="Lambda backend returned an ambiguous render result",
            )
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
        metadata=metadata,
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
