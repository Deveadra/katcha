from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from katcha.editing.blueprints import EditBlueprintContract, persona_commentary_v1
from katcha.integrations.storage import ObjectStore
from katcha.rendering.blueprint_manifest import BlueprintRenderManifest
from katcha.rendering.manifest import ShortRenderManifest
from katcha.rendering.ranked_episode_manifest import RankedEpisodeRenderManifest


RenderKind = Literal["production", "short_episode"]


@dataclass(frozen=True, slots=True)
class RenderQCResult:
    status: Literal["passed"]
    phase: Literal["pre_render", "post_render"]
    manifest_version: str
    output_key: str
    expected_duration_seconds: float
    actual_duration_seconds: float | None
    size_bytes: int | None
    checks: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "manifest_version": self.manifest_version,
            "output_key": self.output_key,
            "expected_duration_seconds": self.expected_duration_seconds,
            "actual_duration_seconds": self.actual_duration_seconds,
            "size_bytes": self.size_bytes,
            "checks": list(self.checks),
        }


def _blueprint(snapshot: dict[str, Any] | None) -> EditBlueprintContract:
    if not snapshot:
        return persona_commentary_v1()
    return EditBlueprintContract.model_validate(snapshot)


def _manifest(
    payload: dict[str, Any],
) -> ShortRenderManifest | RankedEpisodeRenderManifest | BlueprintRenderManifest:
    version = str(payload.get("version") or "")
    if version == "short-render-v1":
        return ShortRenderManifest.model_validate(payload)
    if version == "ranked-episode-render-v1":
        return RankedEpisodeRenderManifest.model_validate(payload)
    if version == "blueprint-render-v1":
        return BlueprintRenderManifest.model_validate(payload)
    raise ValueError(f"unsupported render manifest version for QC: {version or 'missing'}")


def _asset_keys(
    manifest: ShortRenderManifest | RankedEpisodeRenderManifest | BlueprintRenderManifest,
) -> list[str]:
    if isinstance(manifest, ShortRenderManifest):
        return [
            manifest.source.storage_key,
            *(overlay.asset_key for overlay in manifest.overlays),
        ]
    if isinstance(manifest, RankedEpisodeRenderManifest):
        return [
            *(item.source.storage_key for item in manifest.items),
            *(overlay.asset_key for overlay in manifest.overlays),
        ]
    keys = [manifest.source.storage_key]
    if manifest.narration is not None:
        keys.append(manifest.narration.asset_key)
    return keys


def validate_pre_render(
    *,
    manifest_payload: dict[str, Any],
    blueprint_snapshot: dict[str, Any] | None,
    expected_brand_key: str | None,
    store: ObjectStore | None = None,
) -> RenderQCResult:
    manifest = _manifest(manifest_payload)
    blueprint = _blueprint(blueprint_snapshot)
    object_store = store or ObjectStore()

    if manifest.output_duration_seconds > blueprint.quality.max_duration_seconds + 0.00001:
        raise ValueError("render duration exceeds frozen edit blueprint maximum")

    brand_key = manifest.brand.brand_key
    if expected_brand_key and brand_key != expected_brand_key:
        raise ValueError("render manifest brand does not match frozen channel brand")

    if isinstance(manifest, RankedEpisodeRenderManifest):
        if blueprint.source_layout.mode != "full_frame":
            raise ValueError("ranked episodes require a full-frame editing blueprint")
        if blueprint.narration.mode not in {"persona_voice", "explanatory_voice"}:
            raise ValueError("ranked episodes require a voice editing blueprint")
        if blueprint.narration.required and not manifest.overlays:
            raise ValueError("ranked episode blueprint requires narration overlays")
    elif isinstance(manifest, ShortRenderManifest):
        if blueprint.source_layout.mode != "full_frame":
            raise ValueError("short commentary renderer requires a full-frame blueprint")
        if blueprint.narration.mode not in {"persona_voice", "explanatory_voice"}:
            raise ValueError("short commentary renderer cannot execute a text-only blueprint")
        if blueprint.narration.required and not manifest.overlays:
            raise ValueError("short commentary blueprint requires narration overlays")
    else:
        frozen = EditBlueprintContract.model_validate(manifest.blueprint_snapshot)
        if frozen.model_dump(mode="json") != blueprint.model_dump(mode="json"):
            raise ValueError("render manifest blueprint differs from production blueprint lineage")
        if manifest.brand.brand_key != expected_brand_key and expected_brand_key:
            raise ValueError("blueprint render brand differs from production brand lineage")

    keys = _asset_keys(manifest)
    if len(keys) != len(set(keys)):
        raise ValueError("render manifest contains duplicate media asset keys")
    missing = [key for key in keys if not object_store.exists(key)]
    if missing:
        raise ValueError("render input assets are missing: " + ", ".join(missing))
    empty = [key for key in keys if int(object_store.stat(key)["size_bytes"]) <= 0]
    if empty:
        raise ValueError("render input assets are empty: " + ", ".join(empty))

    checks = (
        "manifest_schema",
        "blueprint_compatibility",
        "brand_lineage",
        "duration_policy",
        "input_assets_exist",
        "input_assets_nonempty",
    )
    return RenderQCResult(
        status="passed",
        phase="pre_render",
        manifest_version=manifest.version,
        output_key=manifest.output_key,
        expected_duration_seconds=manifest.output_duration_seconds,
        actual_duration_seconds=None,
        size_bytes=None,
        checks=checks,
    )


def validate_post_render(
    *,
    manifest_payload: dict[str, Any],
    output_key: str,
    actual_duration_seconds: float,
    store: ObjectStore | None = None,
) -> RenderQCResult:
    manifest = _manifest(manifest_payload)
    object_store = store or ObjectStore()

    if output_key != manifest.output_key:
        raise ValueError("renderer output key does not match the frozen manifest")
    if not object_store.exists(output_key):
        raise ValueError("renderer reported success but output object does not exist")

    stat = object_store.stat(output_key)
    size_bytes = int(stat["size_bytes"])
    if size_bytes < 1024:
        raise ValueError("render output is unexpectedly small")
    content_type = str(stat.get("content_type") or "").lower()
    if content_type and content_type not in {"video/mp4", "application/octet-stream"}:
        raise ValueError(f"render output has unexpected content type: {content_type}")

    expected = float(manifest.output_duration_seconds)
    actual = float(actual_duration_seconds)
    tolerance = max(0.5, expected * 0.02)
    if abs(actual - expected) > tolerance:
        raise ValueError(
            f"render duration mismatch: expected {expected:.3f}s, got {actual:.3f}s"
        )

    return RenderQCResult(
        status="passed",
        phase="post_render",
        manifest_version=manifest.version,
        output_key=output_key,
        expected_duration_seconds=expected,
        actual_duration_seconds=actual,
        size_bytes=size_bytes,
        checks=(
            "output_key",
            "output_exists",
            "output_nonempty",
            "output_content_type",
            "output_duration",
        ),
    )


def assert_render_qc_passed(metadata: dict[str, Any] | None) -> None:
    qc = dict((metadata or {}).get("qc") or {})
    if qc.get("status") != "passed" or qc.get("phase") != "post_render":
        raise ValueError("render has not passed deterministic post-render QC")
