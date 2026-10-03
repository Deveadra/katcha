"""Claim-directed asset scouting; acquisition and rights are checked separately."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from katcha.acquisition.web_scout import _normalized_url, _same_grounded_page
from katcha.acquisition_models import DiscoveryCandidate
from katcha.db import session_scope
from katcha.editorial.asset_schemas import AssetLeads, AssetPlan
from katcha.editorial.project_schemas import EditorialDraft
from katcha.editorial.provider import EditorialBlocked, structured_call
from katcha.integrations.download import canonicalize_url
from katcha.models import Clip, SourceItem
from katcha.services.acquisition import latest_rights_assessment
from katcha.services.clip_lifecycle import channel_ids_for_clip
from katcha.services.editorial_runs import checkpoint


def inspect_managed_candidate(url: str, channel_id: uuid.UUID) -> dict:
    """Do not expose another channel's candidate or treat legacy ingest as rights clearance."""
    unknown = {
        "rights_status": "unreviewed",
        "acquired": False,
        "production_eligible": False,
        "discovery_candidate_id": None,
        "rights_assessment_id": None,
        "clip_id": None,
        "sha256": None,
        "duration_seconds": None,
        "rights_checked_at": datetime.now(UTC).isoformat(),
    }
    with session_scope() as session:
        candidate = session.scalar(
            select(DiscoveryCandidate).where(
                DiscoveryCandidate.canonical_url == canonicalize_url(url)
            )
        )
        if candidate is None:
            return unknown
        if str((candidate.candidate_metadata or {}).get("channel_profile_id")) != str(channel_id):
            return unknown
        assessment = latest_rights_assessment(session, candidate.id)
        source = (
            session.get(SourceItem, candidate.source_item_id) if candidate.source_item_id else None
        )
        clip = session.get(Clip, source.clip_id) if source and source.clip_id else None
        available = bool(clip and channel_id in channel_ids_for_clip(session, clip.id))
        cleared = bool(assessment and assessment.production_eligible)
        result = {
            **unknown,
            "discovery_candidate_id": str(candidate.id),
            "rights_status": "cleared" if cleared else "review_required",
            "rights_assessment_id": str(assessment.id) if assessment else None,
            "acquired": available,
            "production_eligible": bool(cleared and available),
        }
        if available:
            result.update(
                clip_id=str(clip.id),
                sha256=clip.sha256,
                duration_seconds=float(clip.duration_seconds or 0),
            )
        return result


def scout_assets(run_id: str, attempt: int) -> dict:
    row = checkpoint(run_id, attempt, stage="sourcing_assets")
    draft = EditorialDraft.model_validate(row.artifacts["input_draft"])
    if not draft.script:
        raise EditorialBlocked("Save a cited script before scouting supporting assets")
    state = dict(row.artifacts.get("asset_scout") or {})
    if not state:
        plan, _ = structured_call(
            run_id,
            attempt,
            "asset-plan",
            "Plan supporting visual material for this cited script. Request official clips/stills, "
            "interviews, earlier material, explanatory diagrams or exact sourced quote cards "
            "where useful. Each request must reference one supplied beat and only that beat's "
            "claim IDs. Avoid repeating the same trailer shot. A discovery lead is not rights "
            "clearance. Do not assert licenses or permission. Fallbacks must preserve meaning.\n"
            + draft.model_dump_json(),
            AssetPlan,
        )
        beats = {beat.id: beat for beat in draft.script}
        for request in plan.requests:
            beat = beats.get(request.beat_id)
            if beat is None or set(request.claim_ids) != set(beat.claim_ids):
                raise EditorialBlocked("Asset request does not preserve its script beat's evidence")
        state = {
            "requests": plan.model_dump(mode="json")["requests"],
            "completed_requests": [],
            "candidates": [],
            "gaps": [],
        }
        checkpoint(run_id, attempt, artifacts={"asset_scout": state})
    searches = state.get("searches", 0)
    for request in state["requests"]:
        if request["id"] in state["completed_requests"]:
            continue
        if request["medium"] in {"quote", "diagram"}:
            # These need a visual compiler, not invented external media or a search call.
            state["gaps"].append(
                {"request_id": request["id"], "reason": "requires_visual_composition"}
            )
        elif searches < row.options.get("max_queries", 12):
            leads, receipt = structured_call(
                run_id,
                attempt,
                f"asset-search:{request['id']}",
                "Search for relevant supporting media for this visual request. Return actual "
                "source pages encountered in search, preferring original publishers. Identify "
                "the intended medium and why it supports the specific claim. Do not invent "
                "download links or claim reuse permission.\n" + json.dumps(request),
                AssetLeads,
                search=True,
            )
            grounded = set(receipt["grounded_urls"])
            accepted = 0
            for lead in leads.leads:
                url = str(lead.url)
                if not _same_grounded_page(url, grounded):
                    continue
                key = hashlib.sha256((request["id"] + _normalized_url(url)).encode()).hexdigest()[
                    :24
                ]
                if any(candidate["id"] == key for candidate in state["candidates"]):
                    continue
                state["candidates"].append(
                    {
                        **lead.model_dump(mode="json"),
                        "id": key,
                        "request_id": request["id"],
                        "beat_id": request["beat_id"],
                        "claim_ids": request["claim_ids"],
                        **inspect_managed_candidate(url, row.channel_profile_id),
                    }
                )
                accepted += 1
            if not accepted:
                state["gaps"].append(
                    {"request_id": request["id"], "reason": "no_grounded_media_leads"}
                )
            searches += 1
            state["searches"] = searches
        else:
            state["gaps"].append({"request_id": request["id"], "reason": "search_budget_exhausted"})
        state["completed_requests"].append(request["id"])
        checkpoint(run_id, attempt, artifacts={"asset_scout": state})
    # Rights can change during a paused run; report their current state at completion.
    for candidate in state["candidates"]:
        candidate.update(inspect_managed_candidate(candidate["url"], row.channel_profile_id))
    checkpoint(
        run_id,
        attempt,
        status="completed",
        stage="asset_candidates_ready",
        artifacts={"asset_scout": state},
    )
    return {"editorial_run_id": run_id, "status": "completed", "stage": "asset_candidates_ready"}
