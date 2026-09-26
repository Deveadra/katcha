#!/usr/bin/env python3
"""Run the final controlled RankSnaxx Brand v2 real-media visual acceptance."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TARGET_BRAND_KEY = "ranksnaxx"
TARGET_BRAND_VERSION = 2
TERMINAL_PREVIEW_FAILURES = {"failed", "rejected"}


def _dotenv_value(key: str, path: Path = Path(".env")) -> str | None:
    if not path.exists():
        return None
    prefix = f"{key}="
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip('"').strip("'")
    return None


class ApiClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 60,
    ) -> dict[str, Any] | list[Any]:
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{method} {path} failed with HTTP {exc.code}: {body}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc
        return json.loads(body) if body else {}

    def get(self, path: str) -> dict[str, Any] | list[Any]:
        return self.request("GET", path)

    def post(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | list[Any]:
        return self.request("POST", path, payload)


def _require_rank_snaxx_channel(client: ApiClient, channel_profile_id: str) -> None:
    summary = client.get(f"/v1/channels/{channel_profile_id}")
    if not isinstance(summary, dict):
        raise RuntimeError("channel summary response is invalid")
    profile = dict(summary.get("profile") or {})
    metadata = dict(profile.get("profile_metadata") or {})
    if str(metadata.get("channel_title") or "").casefold() != "ranksnaxx":
        raise RuntimeError("Brand v2 acceptance is restricted to the RankSnaxx channel")


def _brand_rows(client: ApiClient, channel_profile_id: str) -> list[dict[str, Any]]:
    rows = client.get(f"/v1/channels/{channel_profile_id}/brands")
    if not isinstance(rows, list):
        raise RuntimeError("channel brands response is invalid")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _ensure_staged_brand_v2(
    client: ApiClient,
    channel_profile_id: str,
) -> dict[str, Any]:
    rows = _brand_rows(client, channel_profile_id)
    existing = next(
        (
            row
            for row in rows
            if row.get("brand_key") == TARGET_BRAND_KEY
            and int(row.get("version") or 0) == TARGET_BRAND_VERSION
        ),
        None,
    )
    if existing is not None:
        if existing.get("is_active") is True:
            raise RuntimeError(
                "RankSnaxx Brand v2 is already active; staged visual acceptance "
                "cannot prove a pre-activation review"
            )
        return existing

    candidates = client.get(
        f"/v1/channels/{channel_profile_id}/brand-candidates"
    )
    if not isinstance(candidates, list):
        raise RuntimeError("brand candidate response is invalid")
    candidate = next(
        (
            dict(item)
            for item in candidates
            if isinstance(item, dict)
            and item.get("brand_key") == TARGET_BRAND_KEY
            and int(item.get("version") or 0) == TARGET_BRAND_VERSION
        ),
        None,
    )
    if candidate is None:
        raise RuntimeError(
            "RankSnaxx Brand v2 is neither staged nor available as a built-in candidate"
        )
    contract = candidate.get("contract")
    if not isinstance(contract, dict):
        raise RuntimeError("RankSnaxx Brand v2 candidate has no valid contract")

    staged = client.post(
        f"/v1/channels/{channel_profile_id}/brands",
        {
            "contract": contract,
            "actor": "operator:ranksnaxx-brand-v2-acceptance",
            "hypothesis": (
                "Validate the committed meme_cry reaction overlay against real "
                "RankSnaxx ranked-episode source and voice media before activation."
            ),
        },
    )
    if not isinstance(staged, dict):
        raise RuntimeError("staging Brand v2 returned an invalid response")
    if staged.get("is_active") is True:
        raise RuntimeError("Brand v2 staging unexpectedly activated the brand")
    return staged


def _episode_detail(
    client: ApiClient,
    episode_id: str,
) -> dict[str, Any]:
    detail = client.get(f"/v1/short-episodes/{episode_id}")
    if not isinstance(detail, dict):
        raise RuntimeError("short episode detail response is invalid")
    return detail


def _assert_real_ranked_episode(
    detail: dict[str, Any],
    channel_profile_id: str,
) -> list[int]:
    episode = dict(detail.get("episode") or {})
    if str(episode.get("channel_profile_id") or "") != channel_profile_id:
        raise RuntimeError("preview episode belongs to a different channel")
    if not episode.get("trend_opportunity_id"):
        raise RuntimeError("preview requires a real trend-linked RankSnaxx episode")

    manifest = dict(episode.get("render_manifest") or {})
    if manifest.get("version") != "ranked-episode-render-v1":
        raise RuntimeError(
            "preview episode does not have a frozen ranked-episode-render-v1 manifest"
        )
    overlays = list(manifest.get("overlays") or [])
    sequences = sorted(
        {
            int(item["sequence"])
            for item in overlays
            if isinstance(item, dict) and item.get("sequence") is not None
        }
    )
    if not sequences:
        raise RuntimeError("ranked episode has no narration sequence for reaction timing")

    items = list(detail.get("items") or [])
    if len(items) < 3:
        raise RuntimeError("preview episode has too few ranked items")
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("preview episode contains an invalid ranked item")
        for source in list(item.get("source_snapshot") or []):
            if not isinstance(source, dict):
                continue
            source_url = str(source.get("source_url") or "")
            if "acceptance-media" in source_url:
                raise RuntimeError(
                    "Brand v2 final acceptance refuses synthetic acceptance fixture media"
                )
    return sequences


def _select_line_ref(sequences: list[int], requested: int | None) -> int:
    if requested is not None:
        if requested not in sequences:
            raise RuntimeError(
                f"requested line-ref {requested} is not present; "
                f"available narration sequences: {sequences}"
            )
        return requested
    return sequences[len(sequences) // 2]


def _create_preview(
    client: ApiClient,
    channel_profile_id: str,
    episode_id: str,
    *,
    line_ref: int,
    offset_seconds: float,
    duration_seconds: float,
    anchor: str,
    animation: str,
    scale: float,
) -> dict[str, Any]:
    preview = client.post(
        (
            f"/v1/channels/{channel_profile_id}/brands/"
            f"{TARGET_BRAND_VERSION}/previews"
        ),
        {
            "short_episode_id": episode_id,
            "reaction_cue": {
                "id": "final-ranksnaxx-v2-meme-cry",
                "asset_key": "meme_cry",
                "line_ref": line_ref,
                "offset_seconds": offset_seconds,
                "duration_seconds": duration_seconds,
                "anchor": anchor,
                "animation": animation,
                "scale": scale,
            },
        },
    )
    if not isinstance(preview, dict):
        raise RuntimeError("brand preview registration returned an invalid response")
    return preview


def _wait_for_verified_preview(
    client: ApiClient,
    channel_profile_id: str,
    preview_id: str,
    *,
    timeout_seconds: int,
    interval_seconds: int = 3,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    path = (
        f"/v1/channels/{channel_profile_id}/brand-previews/{preview_id}"
    )
    while time.monotonic() < deadline:
        current = client.get(path)
        if not isinstance(current, dict):
            raise RuntimeError("brand preview status response is invalid")
        last = current
        status = str(current.get("status") or "")
        print(f"brand preview: status={status or '?'}")
        if status == "verified":
            return current
        if status in TERMINAL_PREVIEW_FAILURES:
            raise RuntimeError(
                f"brand preview failed: status={status} error={current.get('error')}"
            )
        time.sleep(interval_seconds)
    raise TimeoutError(
        f"timed out waiting for verified brand preview; last={last}"
    )


def _activate_reviewed_preview(
    client: ApiClient,
    channel_profile_id: str,
    preview_id: str,
) -> dict[str, Any]:
    preview = client.get(
        f"/v1/channels/{channel_profile_id}/brand-previews/{preview_id}"
    )
    if not isinstance(preview, dict):
        raise RuntimeError("brand preview response is invalid")
    if preview.get("status") != "verified":
        raise RuntimeError("Brand v2 activation requires a verified preview")
    if preview.get("brand_key") != TARGET_BRAND_KEY:
        raise RuntimeError("verified preview belongs to a different brand")
    if int(preview.get("brand_version") or 0) != TARGET_BRAND_VERSION:
        raise RuntimeError("verified preview is not RankSnaxx Brand v2")
    if not preview.get("short_episode_id"):
        raise RuntimeError("verified preview is not backed by a ranked ShortEpisode")
    verification = preview.get("verification")
    if not isinstance(verification, dict) or not verification:
        raise RuntimeError("verified preview is missing render verification evidence")

    activated = client.post(
        (
            f"/v1/channels/{channel_profile_id}/brands/"
            f"{TARGET_BRAND_VERSION}/activate"
        ),
        {"actor": "operator:ranksnaxx-brand-v2-visual-acceptance"},
    )
    if not isinstance(activated, dict):
        raise RuntimeError("Brand v2 activation returned an invalid response")
    if activated.get("is_active") is not True:
        raise RuntimeError("Brand v2 did not become active")
    return activated


def run(args: argparse.Namespace) -> dict[str, Any]:
    token = args.token or os.getenv("KATCHA_CONTROL_API_TOKEN") or _dotenv_value(
        "KATCHA_CONTROL_API_TOKEN"
    )
    if not token:
        raise RuntimeError(
            "KATCHA_CONTROL_API_TOKEN is not available; export it or keep it in .env"
        )
    client = ApiClient(args.api_base, token)
    readiness = client.get("/v1/health/ready")
    if not isinstance(readiness, dict) or readiness.get("status") != "ok":
        raise RuntimeError(f"Katcha backend is not ready: {readiness}")

    _require_rank_snaxx_channel(client, args.channel_profile_id)

    if args.activate_reviewed_preview:
        if not args.confirm_visual_acceptance:
            raise RuntimeError(
                "--activate-reviewed-preview requires --confirm-visual-acceptance "
                "after the operator has watched the verified MP4"
            )
        activated = _activate_reviewed_preview(
            client,
            args.channel_profile_id,
            args.activate_reviewed_preview,
        )
        return {
            "channel_profile_id": args.channel_profile_id,
            "preview_id": args.activate_reviewed_preview,
            "brand_key": activated.get("brand_key"),
            "brand_version": activated.get("version"),
            "brand_active": activated.get("is_active"),
            "visual_acceptance_confirmed": True,
        }

    if not args.episode_id:
        raise RuntimeError(
            "--episode-id is required when creating a Brand v2 visual preview"
        )

    staged = _ensure_staged_brand_v2(client, args.channel_profile_id)
    detail = _episode_detail(client, args.episode_id)
    sequences = _assert_real_ranked_episode(detail, args.channel_profile_id)
    line_ref = _select_line_ref(sequences, args.line_ref)
    preview = _create_preview(
        client,
        args.channel_profile_id,
        args.episode_id,
        line_ref=line_ref,
        offset_seconds=args.offset_seconds,
        duration_seconds=args.duration_seconds,
        anchor=args.anchor,
        animation=args.animation,
        scale=args.scale,
    )
    preview_id = str(preview.get("id") or "")
    if not preview_id:
        raise RuntimeError("brand preview did not return an ID")
    if preview.get("status") == "verified":
        verified = preview
    else:
        verified = _wait_for_verified_preview(
            client,
            args.channel_profile_id,
            preview_id,
            timeout_seconds=args.timeout_seconds,
        )

    media_path = (
        f"/v1/channels/{args.channel_profile_id}/"
        f"brand-previews/{preview_id}/media"
    )
    return {
        "channel_profile_id": args.channel_profile_id,
        "episode_id": args.episode_id,
        "brand_key": staged.get("brand_key"),
        "brand_version": staged.get("version"),
        "brand_active": staged.get("is_active"),
        "preview_id": preview_id,
        "preview_status": verified.get("status"),
        "line_ref": line_ref,
        "available_line_refs": sequences,
        "verification": verified.get("verification"),
        "media_path": media_path,
        "media_url": f"{args.api_base.rstrip('/')}{media_path}",
        "next_action": (
            "Watch the verified MP4 in /editing or via media_url. Inspect mobile-safe "
            "placement, caption clearance, timing/rhythm, and platform UI zones. "
            "Only if accepted, rerun with --activate-reviewed-preview "
            f"{preview_id} --confirm-visual-acceptance."
        ),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Stage RankSnaxx Brand v2 and render its meme_cry reaction against a real "
            "frozen ranked episode. Activation is a separate explicit reviewed-preview step."
        )
    )
    result.add_argument("--channel-profile-id", required=True)
    result.add_argument("--episode-id")
    result.add_argument("--api-base", default="http://localhost:8000")
    result.add_argument("--token")
    result.add_argument("--line-ref", type=int)
    result.add_argument("--offset-seconds", type=float, default=0.2)
    result.add_argument("--duration-seconds", type=float, default=1.0)
    result.add_argument(
        "--anchor",
        default="bottom_right",
        choices=("top_left", "top_right", "bottom_left", "bottom_right"),
    )
    result.add_argument(
        "--animation",
        default="pop_bounce",
        choices=("pop_bounce", "fade", "slide"),
    )
    result.add_argument("--scale", type=float, default=0.22)
    result.add_argument("--timeout-seconds", type=int, default=1800)
    result.add_argument("--activate-reviewed-preview")
    result.add_argument(
        "--confirm-visual-acceptance",
        action="store_true",
        help=(
            "Required with --activate-reviewed-preview. Confirms the operator watched "
            "the verified real-media preview and accepts placement/timing/caption clearance."
        ),
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if args.activate_reviewed_preview and args.episode_id:
        print(
            "Brand v2 acceptance failed: --episode-id cannot be combined with "
            "--activate-reviewed-preview",
            file=sys.stderr,
        )
        return 2
    try:
        summary = run(args)
    except Exception as exc:
        print(f"Brand v2 acceptance failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
