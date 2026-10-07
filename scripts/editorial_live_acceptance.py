#!/usr/bin/env python3
"""Run a controlled live Editorial Project acceptance through reviewed private render."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

TERMINAL_RUN_FAILURES = {"blocked", "failed", "cancelled"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def _dotenv_value(key: str, path: Path = Path(".env")) -> str | None:
    if not path.exists():
        return None
    prefix = f"{key}="
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"').strip("'")
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

    def download(self, path: str, destination: Path, *, timeout: int = 300) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(request, timeout=timeout) as response, destination.open("wb") as handle:
                content_type = str(response.headers.get("Content-Type") or "")
                while block := response.read(1024 * 1024):
                    handle.write(block)
                    digest.update(block)
                    size += len(block)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"GET {path} failed with HTTP {exc.code}: {body}"
            ) from exc
        except URLError as exc:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"GET {path} failed: {exc}") from exc
        if size <= 0 or content_type.split(";")[0].strip() != "video/mp4":
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"private preview returned invalid media: content_type={content_type!r} size={size}"
            )
        return {
            "path": str(destination),
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "content_type": content_type,
        }


def _key(label: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\n".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:32]
    return f"editorial-live-{label}-{digest}"


def _validate_source_url(value: str) -> str:
    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.hostname not in YOUTUBE_HOSTS
    ):
        raise ValueError(
            "Live Editorial acceptance currently requires an HTTPS YouTube source URL"
        )
    return raw


def _project_base(channel_id: str, project_id: str) -> str:
    return f"/v1/channels/{channel_id}/editorial-projects/{project_id}"


def _run_path(channel_id: str, project_id: str, run_id: str) -> str:
    return f"{_project_base(channel_id, project_id)}/runs/{run_id}"


def _wait_run(
    client: ApiClient,
    channel_id: str,
    project_id: str,
    run_id: str,
    *,
    timeout_seconds: int,
    interval_seconds: int = 4,
) -> dict[str, Any]:
    path = _run_path(channel_id, project_id, run_id)
    deadline = time.monotonic() + timeout_seconds
    last_state: tuple[str, str] | None = None
    last_report = 0.0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        current = client.get(path)
        if not isinstance(current, dict):
            raise RuntimeError("Editorial run detail returned a non-object response")
        last = current
        status = str(current.get("status") or "")
        stage = str(current.get("stage") or "")
        now = time.monotonic()
        state = (status, stage)
        if state != last_state or now - last_report >= 30:
            print(f"{current.get('target') or 'editorial'}: status={status} stage={stage}")
            last_state = state
            last_report = now
        if status == "completed":
            return current
        if status in TERMINAL_RUN_FAILURES:
            raise RuntimeError(
                f"Editorial run {run_id} stopped: status={status} stage={stage} "
                f"error={current.get('error')}. Saved run receipts remain available."
            )
        time.sleep(interval_seconds)
    raise TimeoutError(f"timed out waiting for Editorial run {run_id}; last={last}")


def _runs(client: ApiClient, base: str) -> list[dict[str, Any]]:
    rows = client.get(f"{base}/runs?limit=100")
    if not isinstance(rows, list):
        raise RuntimeError("Editorial run history returned a non-list response")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _latest_run(
    client: ApiClient,
    base: str,
    *,
    target: str,
    revision: int,
) -> dict[str, Any] | None:
    for row in _runs(client, base):
        if row.get("target") == target and int(row.get("input_revision") or -1) == revision:
            detail = client.get(f"{base}/runs/{row['editorial_run_id']}")
            if not isinstance(detail, dict):
                raise RuntimeError("Editorial run detail returned a non-object response")
            return dict(detail)
    return None


def _start_run(
    client: ApiClient,
    base: str,
    *,
    revision: int,
    target: str,
    key_parts: tuple[object, ...],
    timeout_seconds: int,
    max_queries: int,
    max_model_calls: int,
    max_model_tokens: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    idempotency_key = _key(target, *key_parts)
    project_id = uuid.UUID(str(key_parts[0]))
    run_id = str(uuid.uuid5(project_id, f"editorial-run:{idempotency_key}"))
    try:
        existing = client.get(f"{base}/runs/{run_id}")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        existing = None
    if existing is not None:
        if not isinstance(existing, dict):
            raise RuntimeError("Editorial run detail returned a non-object response")
        status = str(existing.get("status") or "")
        if status == "completed":
            print(f"reusing completed {target} run {run_id}")
            return dict(existing)
        if status in {"queued", "running"}:
            return _wait_run(
                client,
                str(existing["channel_profile_id"]),
                str(existing["project_id"]),
                run_id,
                timeout_seconds=timeout_seconds,
            )
        raise RuntimeError(
            f"exact {target} run {run_id} is {status}; inspect its saved receipts and "
            "resume it explicitly before acceptance continues"
        )

    payload: dict[str, Any] = {
        "idempotency_key": idempotency_key,
        "expected_revision": revision,
        "target": target,
        "max_queries": max_queries,
        "max_model_calls": max_model_calls,
        "max_model_tokens": max_model_tokens,
        "max_elapsed_seconds": min(max(timeout_seconds, 60), 7200),
    }
    payload.update(extra or {})
    started = client.post(f"{base}/runs", payload)
    if not isinstance(started, dict) or not started.get("editorial_run_id"):
        raise RuntimeError(f"starting {target} returned an invalid response")
    return _wait_run(
        client,
        str(started["channel_profile_id"]),
        str(started["project_id"]),
        str(started["editorial_run_id"]),
        timeout_seconds=timeout_seconds,
    )


def _receipt_summary(*runs: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for run in runs:
        calls = dict((run.get("artifacts") or {}).get("provider_calls") or {})
        for key, raw in sorted(calls.items()):
            row = dict(raw or {})
            result.append(
                {
                    "run_id": str(run.get("editorial_run_id") or ""),
                    "target": str(run.get("target") or ""),
                    "call_key": key,
                    "status": row.get("status"),
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "submissions": row.get("submissions"),
                    "input_tokens": row.get("input_tokens"),
                    "output_tokens": row.get("output_tokens"),
                    "coverage": row.get("coverage"),
                    "billing_basis": row.get("billing_basis"),
                    "grounded_url_count": len(row.get("grounded_urls") or []),
                }
            )
    return result


def _ensure_runtime(client: ApiClient) -> dict[str, Any]:
    readiness = client.get("/v1/health/ready")
    if not isinstance(readiness, dict) or readiness.get("status") != "ok":
        raise RuntimeError(f"Katcha backend is not ready: {readiness}")
    runtime = client.get("/v1/runtime/ai")
    if not isinstance(runtime, dict):
        raise RuntimeError("Katcha AI runtime response is invalid")
    if (
        runtime.get("execution_mode") != "live"
        or runtime.get("external_provider_calls_enabled") is not True
    ):
        raise RuntimeError(
            "Live Editorial acceptance requires execution_mode=live and enabled external "
            "provider calls. Fixture/provider-disabled runs do not satisfy E8."
        )
    return runtime


def _ensure_channel(client: ApiClient, channel_id: str) -> dict[str, Any]:
    summary = client.get(f"/v1/channels/{channel_id}")
    if not isinstance(summary, dict):
        raise RuntimeError("channel summary response is invalid")
    profile = dict(summary.get("profile") or {})
    if str(profile.get("id") or channel_id) != channel_id:
        raise RuntimeError("channel summary identity does not match the requested profile")
    brands = client.get(f"/v1/channels/{channel_id}/brands")
    if not isinstance(brands, list) or not any(
        isinstance(item, dict) and item.get("is_active") is True for item in brands
    ):
        raise RuntimeError(
            "Editorial live acceptance requires an active approved channel brand"
        )
    return profile


def _latest_assessment(detail: dict[str, Any]) -> dict[str, Any] | None:
    rows = [dict(row) for row in detail.get("assessments") or [] if isinstance(row, dict)]
    if not rows:
        return None
    return max(rows, key=lambda row: int(row.get("version") or 0))


def _asset_clearance(
    client: ApiClient,
    acquired: dict[str, Any],
    selected_ids: list[str],
) -> list[dict[str, Any]]:
    artifacts = dict(acquired.get("artifacts") or {})
    receipts = dict(artifacts.get("acquired_assets") or {})
    result: list[dict[str, Any]] = []
    for candidate_id in selected_ids:
        receipt = dict(receipts.get(candidate_id) or {})
        discovery_id = str(receipt.get("discovery_candidate_id") or "")
        if not discovery_id:
            raise RuntimeError(f"acquired asset {candidate_id} has no discovery lineage")
        detail = client.get(f"/v1/discovery/candidates/{discovery_id}")
        if not isinstance(detail, dict):
            raise RuntimeError(f"discovery candidate {discovery_id} returned invalid detail")
        assessment = _latest_assessment(detail)
        result.append(
            {
                "candidate_id": candidate_id,
                "discovery_candidate_id": discovery_id,
                "clip_id": str(receipt.get("clip_id") or ""),
                "source_url": str(receipt.get("source_url") or ""),
                "rights_assessment_id": (
                    str(assessment.get("id") or "") if assessment is not None else None
                ),
                "rights_version": (
                    int(assessment.get("version") or 0) if assessment is not None else None
                ),
                "production_eligible": bool(
                    assessment is not None and assessment.get("production_eligible") is True
                ),
            }
        )
    return result


def _ensure_clip_analysis(
    client: ApiClient,
    clip_id: str,
    *,
    timeout_seconds: int,
) -> None:
    try:
        features = client.get(f"/v1/clips/{clip_id}/features")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        features = {}
    if (
        isinstance(features, dict)
        and features.get("contact_sheet_key")
        and features.get("keyframe_keys")
    ):
        return
    analysis = client.post(f"/v1/clips/{clip_id}/analyze", {"force_retry": False})
    if not isinstance(analysis, dict) or not analysis.get("analysis_run_id"):
        raise RuntimeError(f"analysis start returned invalid response for clip {clip_id}")
    if analysis.get("status") == "failed":
        analysis = client.post(f"/v1/clips/{clip_id}/analyze", {"force_retry": True})
        if not isinstance(analysis, dict) or not analysis.get("analysis_run_id"):
            raise RuntimeError(f"analysis retry returned invalid response for clip {clip_id}")
    analysis_id = str(analysis["analysis_run_id"])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = client.get(f"/v1/analysis/{analysis_id}")
        if not isinstance(current, dict):
            raise RuntimeError("analysis detail returned invalid response")
        status = str(current.get("status") or "")
        if status == "completed":
            break
        if status == "failed":
            raise RuntimeError(
                f"supporting clip analysis {analysis_id} failed: {current.get('error')}"
            )
        time.sleep(4)
    else:
        raise TimeoutError(f"timed out waiting for supporting clip analysis {analysis_id}")
    features = client.get(f"/v1/clips/{clip_id}/features")
    if (
        not isinstance(features, dict)
        or not features.get("contact_sheet_key")
        or not features.get("keyframe_keys")
    ):
        raise RuntimeError(
            f"supporting clip {clip_id} completed analysis without sampled-frame evidence"
        )


def _candidate_summary(run: dict[str, Any]) -> list[dict[str, Any]]:
    scout = dict((run.get("artifacts") or {}).get("asset_scout") or {})
    return [
        {
            "id": row.get("id"),
            "medium": row.get("medium"),
            "title": row.get("title"),
            "url": row.get("url"),
            "beat_id": row.get("beat_id"),
            "rights_status": row.get("rights_status"),
            "acquired": row.get("acquired"),
            "production_eligible": row.get("production_eligible"),
            "discovery_candidate_id": row.get("discovery_candidate_id"),
        }
        for row in scout.get("candidates") or []
        if isinstance(row, dict)
    ]


def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = time.monotonic()
    token = args.token or os.getenv("KATCHA_CONTROL_API_TOKEN") or _dotenv_value(
        "KATCHA_CONTROL_API_TOKEN"
    )
    if not token:
        raise RuntimeError(
            "KATCHA_CONTROL_API_TOKEN is not available; export it or keep it in .env"
        )
    client = ApiClient(args.api_base, token)
    runtime = _ensure_runtime(client)
    profile = _ensure_channel(client, args.channel_profile_id)

    if args.project_id:
        project = client.get(
            _project_base(args.channel_profile_id, args.project_id)
        )
        if not isinstance(project, dict):
            raise RuntimeError("Editorial project detail response is invalid")
        project_id = str(project["id"])
        source_url = str((project.get("brief") or {}).get("source_urls", [""])[0])
        print(f"resuming project_id={project_id}")
    else:
        if not args.source_url:
            raise RuntimeError("--source-url is required when creating a live acceptance project")
        if not args.confirm_source_authorized:
            raise RuntimeError(
                "Refusing live source acquisition without --confirm-source-authorized. "
                "Use only media you are authorized to process for this acceptance."
            )
        source_url = _validate_source_url(args.source_url)
        run_key = args.run_key or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        brief: dict[str, Any] = {
            "prompt": args.prompt,
            "source_urls": [source_url],
            "target_duration_seconds": args.target_duration_seconds,
        }
        if args.source_clip_id:
            brief["source_clip_bindings"] = {source_url: args.source_clip_id}
        created = client.post(
            f"/v1/channels/{args.channel_profile_id}/editorial-projects",
            {
                "brief": brief,
                "idempotency_key": f"live-acceptance-{run_key}",
            },
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise RuntimeError("Editorial project creation returned an invalid response")
        project = dict(created)
        project_id = str(project["id"])
        print(f"project_id={project_id}")

    base = _project_base(args.channel_profile_id, project_id)
    project = client.get(base)
    if not isinstance(project, dict):
        raise RuntimeError("Editorial project detail response is invalid")

    script_run: dict[str, Any] | None = None
    if int(project.get("revision") or 0) == 0:
        script_run = _start_run(
            client,
            base,
            revision=0,
            target="script",
            key_parts=(project_id, source_url),
            timeout_seconds=args.timeout_seconds,
            max_queries=args.max_queries,
            max_model_calls=args.max_model_calls,
            max_model_tokens=args.max_model_tokens,
        )
        project = client.get(base)
        if not isinstance(project, dict):
            raise RuntimeError("Editorial project could not be reloaded after script generation")
    revision = int(project.get("revision") or 0)
    if revision <= 0:
        raise RuntimeError("live script run completed without a saved Editorial revision")
    revisions = client.get(f"{base}/revisions?limit=1")
    if not isinstance(revisions, list) or not revisions:
        raise RuntimeError("Editorial project has no frozen script revision")
    revision_row = dict(revisions[0])
    draft = dict(revision_row.get("draft") or {})
    script = list(draft.get("script") or [])
    if not script:
        raise RuntimeError("saved Editorial revision has no script beats")

    if script_run is None:
        script_run = _latest_run(client, base, target="script", revision=0)
    script_review = {
        "revision": revision,
        "digest": revision_row.get("digest"),
        "beats": len(script),
        "claims": len(draft.get("claims") or []),
        "sources": len(draft.get("sources") or []),
        "observations": len(draft.get("observations") or []),
    }
    if not args.continue_after_script_review:
        return {
            "status": "script_review_required",
            "project_id": project_id,
            "channel_profile_id": args.channel_profile_id,
            "source_url": source_url,
            "script_review": script_review,
            "provider_receipts": _receipt_summary(
                *(row for row in [script_run] if row is not None)
            ),
            "next": (
                "Inspect the saved evidence/script in Editorial Studio, then rerun with "
                f"--project-id {project_id} --continue-after-script-review."
            ),
        }

    asset_run = _start_run(
        client,
        base,
        revision=revision,
        target="assets",
        key_parts=(project_id, revision),
        timeout_seconds=args.timeout_seconds,
        max_queries=args.max_queries,
        max_model_calls=args.max_model_calls,
        max_model_tokens=args.max_model_tokens,
    )
    candidates = _candidate_summary(asset_run)
    selected_ids = list(dict.fromkeys(args.asset_candidate_id or []))
    if not selected_ids:
        return {
            "status": "asset_selection_required",
            "project_id": project_id,
            "revision": revision,
            "script_review": script_review,
            "asset_scout_run_id": asset_run["editorial_run_id"],
            "candidates": candidates,
            "provider_receipts": _receipt_summary(script_run, asset_run),
            "next": (
                "Review candidate relevance/source pages, then rerun with one or more "
                "--asset-candidate-id values. Selection is not a rights decision."
            ),
        }
    by_id = {str(row.get("id")): row for row in candidates}
    missing = [identity for identity in selected_ids if identity not in by_id]
    if missing:
        raise RuntimeError(f"selected assets were not discovered by this scout: {missing}")
    if any(by_id[identity].get("medium") != "video" for identity in selected_ids):
        raise RuntimeError(
            "live acceptance acquisition currently selects video candidates only; "
            "use source-frame still capture after a cleared video is acquired"
        )

    acquired = _start_run(
        client,
        base,
        revision=revision,
        target="acquire_assets",
        key_parts=(project_id, revision, asset_run["editorial_run_id"], *selected_ids),
        timeout_seconds=args.timeout_seconds,
        max_queries=args.max_queries,
        max_model_calls=args.max_model_calls,
        max_model_tokens=args.max_model_tokens,
        extra={
            "scout_run_id": asset_run["editorial_run_id"],
            "asset_candidate_ids": selected_ids,
        },
    )
    clearance = _asset_clearance(client, acquired, selected_ids)
    uncleared = [row for row in clearance if not row["production_eligible"]]
    if uncleared:
        return {
            "status": "asset_rights_review_required",
            "project_id": project_id,
            "revision": revision,
            "asset_run_id": acquired["editorial_run_id"],
            "selected_assets": clearance,
            "provider_receipts": _receipt_summary(script_run, asset_run),
            "next": (
                "Use Katcha's normal rights-review flow to record evidence/authorization for "
                "these discovery candidates. Do not infer permission from discovery. Rerun "
                f"with --project-id {project_id} --continue-after-script-review and the same "
                "--asset-candidate-id values after clearance."
            ),
        }

    for row in clearance:
        clip_id = str(row.get("clip_id") or "")
        if not clip_id:
            raise RuntimeError(
                f"cleared asset {row['candidate_id']} has no managed clip receipt"
            )
        _ensure_clip_analysis(
            client,
            clip_id,
            timeout_seconds=args.timeout_seconds,
        )

    direction_run = _start_run(
        client,
        base,
        revision=revision,
        target="direction",
        key_parts=(project_id, revision, acquired["editorial_run_id"]),
        timeout_seconds=args.timeout_seconds,
        max_queries=args.max_queries,
        max_model_calls=args.max_model_calls,
        max_model_tokens=args.max_model_tokens,
        extra={
            "asset_run_id": acquired["editorial_run_id"],
            "direction": {"presentation_mode": "captioned_silent"},
        },
    )
    storyboard = dict((direction_run.get("artifacts") or {}).get("storyboard") or {})
    if not storyboard.get("beats"):
        raise RuntimeError("completed visual direction has no validated storyboard")

    render_run = _start_run(
        client,
        base,
        revision=revision,
        target="render",
        key_parts=(project_id, revision, direction_run["editorial_run_id"]),
        timeout_seconds=args.timeout_seconds,
        max_queries=args.max_queries,
        max_model_calls=args.max_model_calls,
        max_model_tokens=args.max_model_tokens,
        extra={
            "asset_run_id": acquired["editorial_run_id"],
            "direction_run_id": direction_run["editorial_run_id"],
            "storyboard": storyboard,
        },
    )
    if render_run.get("stage") != "render_ready_for_review":
        raise RuntimeError(
            "render completed without reaching render_ready_for_review: "
            f"{render_run.get('stage')}"
        )
    render_id = str(render_run["editorial_run_id"])
    review = client.get(f"{base}/runs/{render_id}/review")
    if not isinstance(review, dict):
        raise RuntimeError("render review status returned an invalid response")
    program_map = client.get(f"{base}/runs/{render_id}/program-map")
    if not isinstance(program_map, dict):
        raise RuntimeError("render program map returned an invalid response")
    preview_path = f"{base}/runs/{render_id}/preview"
    preview_receipt = None
    if args.preview_output:
        preview_receipt = client.download(
            preview_path,
            Path(args.preview_output).expanduser(),
            timeout=args.timeout_seconds,
        )

    summary: dict[str, Any] = {
        "project_id": project_id,
        "channel_profile_id": args.channel_profile_id,
        "channel_title": profile.get("profile_metadata", {}).get("channel_title"),
        "source_url": source_url,
        "revision": revision,
        "script_review": script_review,
        "asset_scout_run_id": asset_run["editorial_run_id"],
        "asset_run_id": acquired["editorial_run_id"],
        "direction_run_id": direction_run["editorial_run_id"],
        "render_run_id": render_id,
        "preview_endpoint": preview_path,
        "preview_file": preview_receipt,
        "program_map": {
            "manifest_version": program_map.get("manifest_version"),
            "fps": program_map.get("fps"),
            "output_duration_seconds": program_map.get("output_duration_seconds"),
            "beat_count": len(program_map.get("beats") or []),
        },
        "selected_assets": clearance,
        "provider_receipts": _receipt_summary(script_run, asset_run, direction_run),
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
    }

    if not args.approve_render_id:
        summary.update(
            status="render_review_required",
            review_status=review.get("status"),
            next=(
                "Inspect the exact private preview and evidence/timing. To approve only this "
                f"render, rerun with --project-id {project_id} --continue-after-script-review "
                f"--approve-render-id {render_id} --confirm-preview-inspected."
            ),
        )
        return summary

    if args.approve_render_id != render_id:
        raise RuntimeError(
            "--approve-render-id does not match the current exact reviewed render "
            f"({render_id}); refusing stale approval"
        )
    if not args.confirm_preview_inspected:
        raise RuntimeError(
            "--approve-render-id also requires --confirm-preview-inspected after human review"
        )
    approved = client.post(
        f"{base}/runs/{render_id}/review",
        {
            "idempotency_key": _key("review-approve", render_id),
            "expected_revision": revision,
            "expected_review_sequence": int(review.get("sequence") or 0),
            "decision": "approve",
            "note": "Live Editorial acceptance: exact private preview inspected and approved.",
        },
    )
    if not isinstance(approved, dict):
        raise RuntimeError("render approval returned an invalid response")
    final_review = client.get(f"{base}/runs/{render_id}/review")
    if (
        not isinstance(final_review, dict)
        or final_review.get("status") != "approve"
        or final_review.get("publication_available") is not True
    ):
        raise RuntimeError(f"render approval did not become current: {final_review}")
    summary.update(
        status="reviewed_render_accepted",
        review_status="approve",
        review_sequence=final_review.get("sequence"),
        publication_available=True,
        next=(
            "Acceptance stops here. Publication remains a separate controlled handoff; "
            "do not infer upload/scheduling approval from this render review."
        ),
    )
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Run live Editorial Projects from an explicitly authorized YouTube source through "
            "research, supporting-media review, frame-grounded direction, deterministic render "
            "and exact human review. Publication is intentionally out of scope."
        )
    )
    result.add_argument("--channel-profile-id", required=True)
    result.add_argument("--api-base", default="http://localhost:8000")
    result.add_argument("--token")
    result.add_argument("--run-key")
    result.add_argument("--project-id", help="Resume an existing Editorial acceptance project.")
    result.add_argument("--source-url")
    result.add_argument("--source-clip-id")
    result.add_argument(
        "--confirm-source-authorized",
        action="store_true",
        help="Confirm you are authorized to process the supplied source for this acceptance.",
    )
    result.add_argument(
        "--prompt",
        default=(
            "Create a concise, evidence-grounded things-you-missed breakdown of this authorized "
            "source. Distinguish confirmed observations, inference and theory."
        ),
    )
    result.add_argument("--target-duration-seconds", type=int, default=120)
    result.add_argument("--timeout-seconds", type=int, default=7200)
    result.add_argument("--max-queries", type=int, default=12)
    result.add_argument("--max-model-calls", type=int, default=30)
    result.add_argument("--max-model-tokens", type=int, default=1_000_000)
    result.add_argument(
        "--continue-after-script-review",
        action="store_true",
        help="Continue only after the saved cited script/evidence has been reviewed.",
    )
    result.add_argument(
        "--asset-candidate-id",
        action="append",
        default=[],
        help="Select a reviewed asset scout candidate. Repeat for multiple assets.",
    )
    result.add_argument(
        "--preview-output",
        help="Optionally download the exact authenticated private preview MP4 for inspection.",
    )
    result.add_argument(
        "--approve-render-id",
        help="Approve only this exact render ID after inspecting its private preview.",
    )
    result.add_argument(
        "--confirm-preview-inspected",
        action="store_true",
        help="Required with --approve-render-id; confirms human inspection actually occurred.",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        summary = run(args)
    except Exception as exc:
        print(f"Editorial live acceptance failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
