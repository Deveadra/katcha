#!/usr/bin/env python3
"""Drive a real, rights-qualified RankSnaxx trend opportunity to private YouTube."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TERMINAL_FAILURES = {"failed", "rejected"}
EDITORIAL_READY = {
    "voiced",
    "review",
    "editorial_approved",
    "rendered",
    "render_review",
    "approved",
}
RENDER_READY = {"rendered", "render_review", "approved"}


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


def _wait(
    label: str,
    getter,
    *,
    accepted: set[str],
    timeout_seconds: int,
    interval_seconds: int = 3,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        current = getter()
        if not isinstance(current, dict):
            raise RuntimeError(f"{label} returned a non-object response")
        last = current
        status = str(current.get("status") or "")
        stage = str(current.get("stage") or "")
        print(f"{label}: status={status or '?'} stage={stage or '-'}")
        if status in accepted:
            return current
        if status in TERMINAL_FAILURES:
            raise RuntimeError(
                f"{label} failed: status={status} stage={stage} "
                f"error={current.get('error')}"
            )
        time.sleep(interval_seconds)
    raise TimeoutError(f"timed out waiting for {label}; last={last}")


def _ensure_rank_snaxx(client: ApiClient, channel_profile_id: str) -> str:
    summary = client.get(f"/v1/channels/{channel_profile_id}")
    if not isinstance(summary, dict):
        raise RuntimeError("channel summary response is invalid")
    profile = dict(summary.get("profile") or {})
    metadata = dict(profile.get("profile_metadata") or {})
    if str(metadata.get("channel_title") or "").casefold() != "ranksnaxx":
        raise RuntimeError("live runner is restricted to the RankSnaxx channel profile")
    connection_id = str(profile.get("youtube_connection_id") or "")
    if not connection_id:
        raise RuntimeError("RankSnaxx channel profile has no YouTube connection")

    brands = client.get(f"/v1/channels/{channel_profile_id}/brands")
    if not isinstance(brands, list) or not any(
        item.get("brand_key") == "ranksnaxx" and item.get("is_active") is True
        for item in brands
        if isinstance(item, dict)
    ):
        raise RuntimeError("RankSnaxx brand contract is not active")
    return connection_id


def _activation_path(
    channel_profile_id: str,
    opportunity_id: str,
    item_count: int,
) -> str:
    query = urlencode({"item_count": item_count})
    return (
        f"/v1/channels/{channel_profile_id}/trends/opportunities/"
        f"{opportunity_id}/activation?{query}"
    )


def _select_ready_opportunity(
    client: ApiClient,
    channel_profile_id: str,
    *,
    item_count: int,
    limit: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    query = urlencode({"limit": limit, "min_score": 0.0})
    opportunities = client.get(
        f"/v1/channels/{channel_profile_id}/trends/opportunities?{query}"
    )
    if not isinstance(opportunities, list):
        raise RuntimeError("trend opportunities response is invalid")
    if not opportunities:
        raise RuntimeError(
            "RankSnaxx has no current trend opportunities; refresh trend intelligence first"
        )

    rejected: list[str] = []
    for opportunity in opportunities:
        if not isinstance(opportunity, dict):
            continue
        opportunity_id = str(opportunity.get("id") or "")
        if not opportunity_id:
            continue
        try:
            preview = client.get(
                _activation_path(channel_profile_id, opportunity_id, item_count)
            )
        except RuntimeError as exc:
            rejected.append(f"{opportunity_id}: preview failed ({exc})")
            continue
        if not isinstance(preview, dict):
            rejected.append(f"{opportunity_id}: invalid preview")
            continue
        if preview.get("ready") is True:
            return opportunity, preview
        eligible = len(list(preview.get("eligible") or []))
        reason = str(preview.get("readiness_reason") or "not_ready")
        rejected.append(
            f"{opportunity_id}: {reason} "
            f"(eligible={eligible}, required={preview.get('item_count')})"
        )

    detail = "; ".join(rejected[:8])
    raise RuntimeError(
        "no activation-ready RankSnaxx trend opportunity was found. "
        "Katcha needs enough promoted, ingested, scored, rights-qualified clips. "
        f"Checked: {detail}"
    )


def _assert_real_episode(detail: dict[str, Any]) -> None:
    episode = dict(detail.get("episode") or {})
    if not episode.get("trend_opportunity_id"):
        raise RuntimeError(
            "live production requires an episode frozen from a real trend opportunity"
        )
    items = list(detail.get("items") or [])
    if len(items) < 3:
        raise RuntimeError("live production episode has too few ranked items")

    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("live production episode contains an invalid item")
        acquisition = dict(item.get("acquisition_snapshot") or {})
        if acquisition.get("eligible") is not True:
            raise RuntimeError("live production episode contains an ineligible clip")
        if not acquisition.get("candidate_id"):
            raise RuntimeError("live production clip is missing discovery lineage")
        for source in list(item.get("source_snapshot") or []):
            if not isinstance(source, dict):
                continue
            source_url = str(source.get("source_url") or "")
            if "acceptance-media" in source_url:
                raise RuntimeError(
                    "live production runner refuses synthetic acceptance fixture media"
                )


def _publication_payload(connection_id: str, premise: str) -> dict[str, Any]:
    return {
        "youtube_connection_id": connection_id,
        "title": f"[PRIVATE FIRST PRODUCTION] {premise}"[:100],
        "description": (
            "Katcha first real RankSnaxx production acceptance. "
            "Rights-qualified source media. PRIVATE review copy."
        ),
        "tags": ["ranksnaxx", "private-review", "katcha"],
        "category_id": "24",
        "privacy_status": "private",
        "publish_at": None,
        "notify_subscribers": False,
        "made_for_kids": False,
        "contains_synthetic_media": False,
    }


def _episode_detail(client: ApiClient, episode_id: str) -> dict[str, Any]:
    detail = client.get(f"/v1/short-episodes/{episode_id}")
    if not isinstance(detail, dict):
        raise RuntimeError("short episode detail response is invalid")
    return detail


def _resume_episode(
    client: ApiClient,
    channel_profile_id: str,
    episode_id: str,
) -> tuple[dict[str, Any], str]:
    detail = _episode_detail(client, episode_id)
    episode = dict(detail.get("episode") or {})
    if str(episode.get("channel_profile_id") or "") != channel_profile_id:
        raise RuntimeError("resume episode belongs to a different channel profile")
    _assert_real_episode(detail)
    opportunity_id = str(episode.get("trend_opportunity_id") or "")
    return detail, opportunity_id


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

    connection_id = _ensure_rank_snaxx(client, args.channel_profile_id)

    if args.episode_id:
        detail, opportunity_id = _resume_episode(
            client,
            args.channel_profile_id,
            args.episode_id,
        )
        episode = dict(detail["episode"])
        episode_id = str(episode["id"])
        premise = str(episode.get("premise") or "RankSnaxx live production")
        print(f"resuming episode_id={episode_id}")
    else:
        if args.opportunity_id:
            opportunity_id = args.opportunity_id
            preview = client.get(
                _activation_path(
                    args.channel_profile_id,
                    opportunity_id,
                    args.item_count,
                )
            )
            if not isinstance(preview, dict) or preview.get("ready") is not True:
                raise RuntimeError(
                    f"requested trend opportunity is not activation-ready: {preview}"
                )
        else:
            opportunity, preview = _select_ready_opportunity(
                client,
                args.channel_profile_id,
                item_count=args.item_count,
                limit=args.opportunity_limit,
            )
            opportunity_id = str(opportunity["id"])

        activation = client.post(
            (
                f"/v1/channels/{args.channel_profile_id}/trends/opportunities/"
                f"{opportunity_id}/activate"
            ),
            {
                "item_count": args.item_count,
                "actor": "operator:ranksnaxx-live-production",
            },
        )
        if not isinstance(activation, dict):
            raise RuntimeError("trend activation response is invalid")
        episode_id = str(activation.get("short_episode_id") or "")
        if not episode_id:
            raise RuntimeError("trend activation did not return a short episode ID")
        detail = _episode_detail(client, episode_id)
        _assert_real_episode(detail)
        episode = dict(detail["episode"])
        premise = str(episode.get("premise") or "RankSnaxx live production")
        print(
            f"selected opportunity_id={opportunity_id} "
            f"episode_id={episode_id}"
        )
        client.post(
            (
                f"/v1/channels/{args.channel_profile_id}/trends/explorer/"
                f"{opportunity_id}/editorial"
            ),
            {"episode_id": episode_id},
        )

    current_status = str(episode.get("status") or "")
    if current_status in EDITORIAL_READY:
        voiced = episode
    else:
        voiced = _wait(
            "episode editorial",
            lambda: dict(_episode_detail(client, episode_id)["episode"]),
            accepted=EDITORIAL_READY,
            timeout_seconds=args.timeout_seconds,
        )

    if not args.approve_render and not args.approve_private_upload:
        return {
            "channel_profile_id": args.channel_profile_id,
            "trend_opportunity_id": opportunity_id,
            "episode_id": episode_id,
            "status": voiced.get("status"),
            "stage": voiced.get("stage"),
            "next_action": (
                f"Inspect episode {episode_id}, then resume with --episode-id "
                f"{episode_id} --approve-render."
            ),
        }

    if voiced.get("status") in {"voiced", "review"}:
        client.post(
            f"/v1/short-episodes/{episode_id}/review",
            {
                "decision": "approve",
                "note": "Approve first real RankSnaxx editorial for private render review.",
                "actor": "operator:ranksnaxx-live-production",
            },
        )

    post_editorial = _episode_detail(client, episode_id)
    rendered = dict(post_editorial["episode"])
    if rendered.get("status") not in RENDER_READY:
        rendered = _wait(
            "episode render",
            lambda: dict(_episode_detail(client, episode_id)["episode"]),
            accepted=RENDER_READY,
            timeout_seconds=args.timeout_seconds,
        )

    if not args.approve_private_upload:
        return {
            "channel_profile_id": args.channel_profile_id,
            "trend_opportunity_id": opportunity_id,
            "episode_id": episode_id,
            "status": rendered.get("status"),
            "stage": rendered.get("stage"),
            "private_upload_started": False,
        }

    detail = _episode_detail(client, episode_id)
    _assert_real_episode(detail)
    rendered = dict(detail["episode"])
    if rendered.get("status") in {"rendered", "render_review"}:
        client.post(
            f"/v1/short-episodes/{episode_id}/review",
            {
                "decision": "approve",
                "note": "Approve verified first real RankSnaxx render for PRIVATE upload.",
                "actor": "operator:ranksnaxx-live-production",
            },
        )

    publication = client.post(
        f"/v1/short-episodes/{episode_id}/publications",
        _publication_payload(connection_id, premise),
    )
    if not isinstance(publication, dict):
        raise RuntimeError("publication response is invalid")
    publication_id = str(publication.get("id") or "")
    if not publication_id:
        raise RuntimeError("publication did not return an ID")

    final = _wait(
        "YouTube publication",
        lambda: client.get(f"/v1/publications/{publication_id}"),
        accepted={"private"},
        timeout_seconds=args.timeout_seconds,
        interval_seconds=5,
    )
    if final.get("privacy_status") != "private" or final.get("status") != "private":
        raise RuntimeError(f"live publication did not finish private: {final}")

    return {
        "channel_profile_id": args.channel_profile_id,
        "trend_opportunity_id": opportunity_id,
        "episode_id": episode_id,
        "publication_id": publication_id,
        "youtube_video_id": final.get("youtube_video_id"),
        "status": final.get("status"),
        "privacy_status": final.get("privacy_status"),
        "synthetic_fixture": False,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Drive an activation-ready, rights-qualified RankSnaxx trend opportunity "
            "through AI editorial, render review, and an optional PRIVATE YouTube upload."
        )
    )
    result.add_argument("--channel-profile-id", required=True)
    result.add_argument("--api-base", default="http://localhost:8000")
    result.add_argument("--token")
    result.add_argument("--opportunity-id")
    result.add_argument("--episode-id")
    result.add_argument("--item-count", type=int, default=5, choices=(3, 5, 7))
    result.add_argument("--opportunity-limit", type=int, default=25)
    result.add_argument("--timeout-seconds", type=int, default=1800)
    result.add_argument(
        "--approve-render",
        action="store_true",
        help=(
            "Approve the editorial gate and render the real episode, then stop for "
            "render review without uploading."
        ),
    )
    result.add_argument(
        "--approve-private-upload",
        action="store_true",
        help=(
            "Approve both review gates and upload the verified real episode to the "
            "connected RankSnaxx channel as PRIVATE only."
        ),
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if args.episode_id and args.opportunity_id:
        print(
            "live production failed: --episode-id and --opportunity-id are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    try:
        summary = run(args)
    except Exception as exc:
        print(f"live production failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
