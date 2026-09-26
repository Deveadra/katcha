#!/usr/bin/env python3
"""Run a controlled RankSnaxx private-upload acceptance against a live Katcha stack."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TERMINAL_FAILURES = {"failed", "rejected"}
SOURCE_BASE = "http://acceptance-media:8090"


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

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | list[Any]:
        return self.request("POST", path, payload)


def _wait(
    label: str,
    getter,
    *,
    accepted: set[str],
    timeout_seconds: int,
    interval_seconds: int = 3,
    no_progress_seconds: int | None = None,
    no_progress_hint: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    last_state: tuple[str, str] | None = None
    state_since = time.monotonic()
    last_report = 0.0

    while time.monotonic() < deadline:
        current = getter()
        if not isinstance(current, dict):
            raise RuntimeError(f"{label} returned a non-object response")

        last = current
        status = str(current.get("status") or "")
        stage = str(current.get("stage") or "")
        state = (status, stage)
        now = time.monotonic()

        if state != last_state:
            state_since = now
            last_state = state
            print(f"{label}: status={status or '?'} stage={stage or '-'}")
            last_report = now
        elif now - last_report >= 30:
            unchanged = int(now - state_since)
            print(
                f"{label}: status={status or '?'} stage={stage or '-'} "
                f"(unchanged for {unchanged}s)"
            )
            last_report = now

        if status in accepted:
            return current
        if status in TERMINAL_FAILURES:
            raise RuntimeError(
                f"{label} failed: status={status} stage={stage} "
                f"error={current.get('error')}"
            )
        if no_progress_seconds is not None and now - state_since >= no_progress_seconds:
            hint = f" {no_progress_hint}" if no_progress_hint else ""
            raise RuntimeError(
                f"{label} made no progress for {no_progress_seconds}s: "
                f"status={status or '?'} stage={stage or '-'}.{hint}"
            )

        time.sleep(interval_seconds)

    raise TimeoutError(f"timed out waiting for {label}; last={last}")


def _fixture_signals(index: int) -> dict[str, float]:
    profiles = [
        dict(hook_strength=96, visual_clarity=94, payoff_strength=76,
             escalation_value=70, commentary_opportunity=84, novelty=76, source_quality=100),
        dict(hook_strength=84, visual_clarity=90, payoff_strength=82,
             escalation_value=78, commentary_opportunity=86, novelty=80, source_quality=100),
        dict(hook_strength=82, visual_clarity=88, payoff_strength=88,
             escalation_value=86, commentary_opportunity=88, novelty=84, source_quality=100),
        dict(hook_strength=88, visual_clarity=92, payoff_strength=95,
             escalation_value=94, commentary_opportunity=90, novelty=88, source_quality=100),
        dict(hook_strength=80, visual_clarity=91, payoff_strength=100,
             escalation_value=99, commentary_opportunity=92, novelty=96, source_quality=100),
    ]
    return profiles[index]


def _publication_payload(connection_id: str, title: str) -> dict[str, Any]:
    return {
        "youtube_connection_id": connection_id,
        "title": title[:100],
        "description": (
            "Katcha controlled end-to-end acceptance upload. "
            "Synthetic fixture media. PRIVATE — do not publish."
        ),
        "tags": ["katcha", "ranksnaxx", "acceptance-test"],
        "category_id": "24",
        "privacy_status": "private",
        "publish_at": None,
        "notify_subscribers": False,
        "made_for_kids": False,
        "contains_synthetic_media": True,
    }


def _ensure_rank_snaxx(client: ApiClient, channel_profile_id: str) -> str:
    summary = client.get(f"/v1/channels/{channel_profile_id}")
    if not isinstance(summary, dict):
        raise RuntimeError("channel summary response is invalid")
    profile = dict(summary.get("profile") or {})
    metadata = dict(profile.get("profile_metadata") or {})
    if str(metadata.get("channel_title") or "").casefold() != "ranksnaxx":
        raise RuntimeError("acceptance runner is restricted to the RankSnaxx channel profile")
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


def _ensure_fixture_clip(
    client: ApiClient,
    index: int,
    timeout_seconds: int,
) -> str:
    source_url = f"{SOURCE_BASE}/clip-{index + 1}.mp4"
    candidate = client.post(
        "/v1/discovery/candidates",
        {
            "source_url": source_url,
            "adapter_key": "manifest",
            "external_id": f"ranksnaxx-acceptance-{index + 1}",
            "title": f"RankSnaxx acceptance fixture {index + 1}",
            "creator": "Katcha acceptance harness",
            "provenance_confidence": 1.0,
            "provenance_claims": {
                "generated_by": "docker-compose.acceptance.yml",
                "synthetic_fixture": True,
            },
            "metadata": {"acceptance_fixture": True, "fixture_index": index + 1},
        },
    )
    assert isinstance(candidate, dict)
    candidate_id = str(candidate["id"])

    detail = client.get(f"/v1/discovery/candidates/{candidate_id}")
    assert isinstance(detail, dict)
    assessments = list(detail.get("assessments") or [])
    eligible = any(
        isinstance(item, dict) and item.get("production_eligible") is True
        for item in assessments
    )
    if not eligible:
        client.post(
            f"/v1/discovery/candidates/{candidate_id}/assessments",
            {
                "rights_basis": "owned",
                "audio_status": "original",
                "originality_gate": "cleared",
                "risk_flags": [],
                "operator_authorized": True,
                "fair_use_factors": {},
                "metadata": {
                    "acceptance_fixture": True,
                    "generated_locally": True,
                },
                "actor": "operator:ranksnaxx-private-acceptance",
                "reason": "Locally generated synthetic fixture owned by the acceptance run.",
            },
        )

    promoted = client.post(
        f"/v1/discovery/candidates/{candidate_id}/promote",
        {"actor": "operator:ranksnaxx-private-acceptance"},
    )
    assert isinstance(promoted, dict)
    source_id = str(promoted["source_id"])

    source = _wait(
        f"source {index + 1}",
        lambda: client.get(f"/v1/sources/{source_id}"),
        accepted={"ready"},
        timeout_seconds=timeout_seconds,
    )
    clip_id = str(source.get("clip_id") or "")
    if not clip_id:
        raise RuntimeError(f"fixture source {index + 1} completed without a clip ID")

    try:
        features = client.get(f"/v1/clips/{clip_id}/features")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        features = {}
    if not isinstance(features, dict) or features.get("candidate_score") is None:
        analysis = client.post(f"/v1/clips/{clip_id}/analyze", {"force_retry": False})
        assert isinstance(analysis, dict)
        if str(analysis.get("status") or "") == "failed":
            print(f"analysis {index + 1}: retrying failed prior run")
            analysis = client.post(
                f"/v1/clips/{clip_id}/analyze",
                {"force_retry": True},
            )
            assert isinstance(analysis, dict)
        analysis_id = str(analysis["analysis_run_id"])
        _wait(
            f"analysis {index + 1}",
            lambda: client.get(f"/v1/analysis/{analysis_id}"),
            accepted={"completed"},
            timeout_seconds=timeout_seconds,
        )

    features = client.get(f"/v1/clips/{clip_id}/features")
    if not isinstance(features, dict) or features.get("candidate_score") is None:
        raise RuntimeError(f"fixture clip {index + 1} has no completed candidate score")
    return clip_id


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

    runtime = client.get("/v1/runtime/ai")
    if not isinstance(runtime, dict):
        raise RuntimeError("Katcha AI runtime response is invalid")
    print(
        "ai runtime: execution_mode="
        f"{runtime.get('execution_mode')} "
        f"routing={runtime.get('live_routing_mode')} "
        f"external_provider_calls={runtime.get('external_provider_calls_enabled')}"
    )

    connection_id = _ensure_rank_snaxx(client, args.channel_profile_id)
    premise = args.premise

    if args.episode_id:
        detail = client.get(f"/v1/short-episodes/{args.episode_id}")
        if not isinstance(detail, dict):
            raise RuntimeError("short episode detail response is invalid")
        episode = dict(detail["episode"])
        if str(episode.get("channel_profile_id")) != args.channel_profile_id:
            raise RuntimeError("resume episode belongs to a different channel profile")
        episode_id = str(episode["id"])
        premise = str(episode.get("premise") or premise)
        print(f"resuming episode_id={episode_id}")
    else:
        clip_ids = [
            _ensure_fixture_clip(client, index, args.timeout_seconds)
            for index in range(5)
        ]
        candidates = [
            {"clip_id": clip_id, **_fixture_signals(index)}
            for index, clip_id in enumerate(clip_ids)
        ]
        run_key = args.run_key or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        created = client.post(
            "/v1/short-episodes",
            {
                "channel_profile_id": args.channel_profile_id,
                "premise": premise,
                "candidates": candidates,
                "item_count": 5,
                "idempotency_key": f"private-acceptance-{run_key}",
            },
        )
        assert isinstance(created, dict)
        episode = dict(created["episode"])
        episode_id = str(episode["id"])
        print(f"episode_id={episode_id}")

        editorial_start = client.post(
            f"/v1/short-episodes/{episode_id}/editorial",
            {"start_stage": "script"},
        )
        assert isinstance(editorial_start, dict)
        editorial_workflow_id = str(editorial_start.get("workflow_id") or "")
        print(
            "editorial workflow_id="
            f"{editorial_workflow_id or '(missing from API response)'}"
        )

    current_status = str(episode.get("status") or "")
    if current_status in {"voiced", "review", "editorial_approved", "rendered", "render_review", "approved"}:
        voiced = episode
    else:
        voiced = _wait(
            "episode editorial",
            lambda: dict(client.get(f"/v1/short-episodes/{episode_id}"))["episode"],
            accepted={"voiced", "review", "editorial_approved", "rendered", "render_review", "approved"},
            timeout_seconds=args.timeout_seconds,
            no_progress_seconds=90,
            no_progress_hint=(
                "If this is still planned/planned, the editorial Temporal workflow "
                "was accepted but no production worker activity has started. Check "
                "the production-worker container and its logs."
            ),
        )

    if not args.approve_render and not args.approve_private_upload:
        return {
            "channel_profile_id": args.channel_profile_id,
            "episode_id": episode_id,
            "status": voiced.get("status"),
            "stage": voiced.get("stage"),
            "next_action": (
                f"Resume this episode with --episode-id {episode_id} --approve-render, "
                "or --approve-private-upload for the full private publishing acceptance."
            ),
        }

    if voiced.get("status") in {"voiced", "review"}:
        client.post(
            f"/v1/short-episodes/{episode_id}/review",
            {
                "decision": "approve",
                "note": "Approve synthetic acceptance editorial for private render test.",
                "actor": "operator:ranksnaxx-private-acceptance",
            },
        )

    post_editorial = client.get(f"/v1/short-episodes/{episode_id}")
    assert isinstance(post_editorial, dict)
    rendered = dict(post_editorial["episode"])
    if rendered.get("status") not in {"rendered", "render_review", "approved"}:
        try:
            rendered = _wait(
                "episode render",
                lambda: dict(client.get(f"/v1/short-episodes/{episode_id}"))["episode"],
                accepted={"rendered", "render_review", "approved"},
                timeout_seconds=args.timeout_seconds,
            )
        except RuntimeError as exc:
            attempts = client.get(
                f"/v1/short-episodes/{episode_id}/render-attempts"
            )
            latest = (
                dict(attempts[-1])
                if isinstance(attempts, list) and attempts
                else {}
            )
            if latest:
                raise RuntimeError(
                    f"{exc}; render_attempt status={latest.get('status')} "
                    f"stage={latest.get('stage')} "
                    f"failure_class={latest.get('last_failure_class')} "
                    f"error={latest.get('error')}"
                ) from exc
            raise
    if not args.approve_private_upload:
        return {
            "channel_profile_id": args.channel_profile_id,
            "episode_id": episode_id,
            "status": rendered.get("status"),
            "stage": rendered.get("stage"),
            "private_upload_started": False,
        }

    if rendered.get("status") in {"rendered", "render_review"}:
        client.post(
            f"/v1/short-episodes/{episode_id}/review",
            {
                "decision": "approve",
                "note": "Approve verified synthetic acceptance render for private upload only.",
                "actor": "operator:ranksnaxx-private-acceptance",
            },
        )

    title = f"[PRIVATE ACCEPTANCE] {premise}"
    publication = client.post(
        f"/v1/short-episodes/{episode_id}/publications",
        _publication_payload(connection_id, title),
    )
    assert isinstance(publication, dict)
    publication_id = str(publication["id"])

    final = _wait(
        "YouTube publication",
        lambda: client.get(f"/v1/publications/{publication_id}"),
        accepted={"private"},
        timeout_seconds=args.timeout_seconds,
        interval_seconds=5,
    )
    if final.get("privacy_status") != "private" or final.get("status") != "private":
        raise RuntimeError(f"acceptance publication did not finish private: {final}")

    return {
        "channel_profile_id": args.channel_profile_id,
        "episode_id": episode_id,
        "publication_id": publication_id,
        "youtube_video_id": final.get("youtube_video_id"),
        "status": final.get("status"),
        "privacy_status": final.get("privacy_status"),
        "synthetic_fixture": True,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Drive five locally generated, rights-safe fixture clips through RankSnaxx "
            "discovery, rights gating, ingest, analysis, AI editorial, render, and an "
            "optional PRIVATE YouTube upload."
        )
    )
    result.add_argument("--channel-profile-id", required=True)
    result.add_argument("--api-base", default="http://localhost:8000")
    result.add_argument("--token")
    result.add_argument("--run-key")
    result.add_argument(
        "--episode-id",
        help=(
            "Resume an existing RankSnaxx acceptance episode instead of generating "
            "fixtures and creating a new episode."
        ),
    )
    result.add_argument(
        "--premise",
        default="Five synthetic test clips that keep escalating",
    )
    result.add_argument("--timeout-seconds", type=int, default=1800)
    result.add_argument(
        "--approve-render",
        action="store_true",
        help=(
            "Explicitly approve the synthetic editorial review gate, render the episode, "
            "and stop at the render-review boundary without uploading to YouTube."
        ),
    )
    result.add_argument(
        "--approve-private-upload",
        action="store_true",
        help=(
            "Explicitly approve both synthetic acceptance review gates and upload the "
            "verified render to the connected RankSnaxx channel as PRIVATE only. This "
            "also implies --approve-render."
        ),
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        summary = run(args)
    except Exception as exc:
        print(f"acceptance failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
