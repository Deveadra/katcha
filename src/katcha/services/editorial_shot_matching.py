"""Durable, deterministic requirement proposals over current scoped measured evidence.

No provider calls, decoder calls, semantic scene inference, or Editorial mutations.
Operator decisions are append-only and remain distinct from generated proposals.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError

from katcha.db import session_scope
from katcha.editorial.media_intake.scene_index import (
    MAX_SEARCH_BYTES,
    _source,
    get_project_scene_index,
)
from katcha.editorial.media_intake.scene_schemas import SceneIndexV1
from katcha.editorial.source_intelligence.contracts import digest
from katcha.editorial.source_intelligence.requirements import AssetManifestV1
from katcha.editorial.source_intelligence.shot_contracts import (
    MatchRequirementsRequest,
    RequirementBindingV1,
    RequirementShotResultV1,
    SelectShotRequest,
    ShotProposalPackageV1,
)
from katcha.editorial_models import EditorialRevision, EditorialRun
from katcha.integrations.storage import ObjectStore
from katcha.services.channel_profiles import ensure_active_profile
from katcha.services.editorial_media_bin import get_project_media_item
from katcha.services.editorial_media_preparation import _lock_project, _seed
from katcha.services.editorial_projects import EditorialConflict, EditorialNotFound
from katcha.shot_matching_models import OperatorShotChoice, RequirementShotPackage

POLICY = "requirement-shot-v1"
MAX_PROPOSALS = 4096
VISUAL_TOKENS = {"red", "green", "blue", "dark", "bright", "mid-tone"}
LIMITS = (
    "Only the shot's first observed frame contributes visual tags.",
    "Visual-change intervals can contain missed or extra cuts.",
    "Characters, actions, scenes, OCR, speech and sustained appearance are unobserved.",
    "Ranking scores are deterministic evidence priorities, not probabilities.",
    "Operator selection does not grant rights or semantic verification.",
)


def _manifest(
    channel: uuid.UUID, project: uuid.UUID, preparation: uuid.UUID, expected_revision: int
) -> AssetManifestV1:
    with session_scope() as session:
        ensure_active_profile(session, channel)
        row = _lock_project(session, channel, project)
        if row.revision != expected_revision:
            raise EditorialConflict("Script changed; regenerate proposals for the saved revision")
        run = session.get(EditorialRun, preparation)
        if (
            run is None
            or run.channel_profile_id != channel
            or run.project_id != project
            or run.options.get("target") != "prepare_media"
        ):
            raise EditorialNotFound("Media preparation not found in this project/channel")
        if run.input_revision != expected_revision:
            raise EditorialConflict("Preparation belongs to a different script revision")
        manifest = AssetManifestV1.model_validate(run.artifacts.get("manifest"))
        seed, _ = _seed(row, session.get(EditorialRevision, (project, row.revision)))
        if (
            manifest.scope.channel_id != channel
            or manifest.scope.project_id != project
            or manifest.scope.revision_digest != seed.content_sha256
        ):
            raise EditorialConflict("Prepared requirements do not match the preserved script")
        return manifest


def _item_fingerprint(item: Any) -> str:
    return digest(
        {
            key: value
            for key, value in vars(item).items()
            if not key.startswith("_") and key not in {"created_at", "last_verified_at"}
        }
        | {
            "id": str(item.id),
            "project_id": str(item.project_id),
            "channel_profile_id": str(item.channel_profile_id),
            "clip_id": str(item.clip_id) if item.clip_id else None,
            "request_id": str(item.request_id),
            "rights_assessment_id": str(item.rights_assessment_id)
            if item.rights_assessment_id
            else None,
        }
    )


def _snapshot(
    channel: uuid.UUID,
    project: uuid.UUID,
    item_ids: tuple[uuid.UUID, ...],
    *,
    store: ObjectStore | None,
    verify_bytes: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    # Scope the entire caller selection before accessing even the first object's bytes.
    items = [get_project_media_item(channel, project, key) for key in sorted(item_ids, key=str)]
    fingerprints = {item.id: _item_fingerprint(item) for item in items}
    sources = {}
    gaps: dict[str, str] = {}
    for item in items:
        try:
            sources[item.id] = _source(channel, project, item.id)
        except EditorialConflict:
            gaps[str(item.id)] = "inspection_permission_or_integrity_evidence_unavailable"
    if (
        sum(
            source.declared_size
            for source in {source.clip_id: source for source in sources.values()}.values()
        )
        > MAX_SEARCH_BYTES
    ):
        raise EditorialConflict("Selection exceeds the bounded media verification byte budget")
    observed = []
    verified: set[uuid.UUID] = set()
    for item in items:
        source = sources.get(item.id)
        if source is None:
            continue
        try:
            index = get_project_scene_index(
                channel,
                project,
                item.id,
                store=store,
                verify_bytes=verify_bytes and source.clip_id not in verified,
            )
        except EditorialNotFound:
            gaps[str(item.id)] = "current_measured_index_missing"
            continue
        # Corruption, revocation during verification and other freshness races fail the request.
        verified.add(source.clip_id)
        if index["status"] != "completed":
            gaps[str(item.id)] = "measured_index_unfinished_or_failed"
            continue
        evidence = SceneIndexV1.model_validate(index["evidence"])
        observed.append({"item": item, "source": source, "index": index, "evidence": evidence})
    _recheck(channel, project, items, fingerprints, sources)
    stamp = digest(
        {
            "items": {str(key): value for key, value in fingerprints.items()},
            "sources": {
                str(key): {**asdict(value), "clip_id": str(value.clip_id)}
                for key, value in sources.items()
            },
            "indexes": {
                str(row["item"].id): {
                    "id": row["index"]["index_id"],
                    "evidence": row["index"]["evidence"],
                }
                for row in observed
            },
            "gaps": gaps,
        }
    )
    return observed, gaps, stamp


def _recheck(
    channel: uuid.UUID,
    project: uuid.UUID,
    items: list[Any],
    fingerprints: dict[uuid.UUID, str],
    sources: dict[Any, Any],
) -> None:
    for item in items:
        if (
            _item_fingerprint(get_project_media_item(channel, project, item.id))
            != fingerprints[item.id]
        ):
            raise EditorialConflict("Media Bin choices changed; reload before matching")
        if item.id in sources and _source(channel, project, item.id) != sources[item.id]:
            raise EditorialConflict("Current source or permission changed during matching")


def _rank(
    manifest: AssetManifestV1,
    revision: int,
    preparation: uuid.UUID,
    observed: list[dict[str, Any]],
    item_gaps: dict[str, str],
    preparation_links: dict[str, set[str]],
) -> ShotProposalPackageV1:
    results = []
    remaining = MAX_PROPOSALS
    for row in observed:
        row["evidence_digest"] = digest(row["index"]["evidence"])
    for requirement in manifest.requirements:
        terms = set(requirement.intent.casefold().split())
        visual_only = bool(terms) and terms <= VISUAL_TOKENS and requirement.kind == "footage"
        candidates = []
        gaps = [] if visual_only else ["semantic_intent_unobserved"]
        if requirement.kind != "footage":
            gaps.append("required_media_kind_not_observed_by_video_index")
        if remaining == 0:
            results.append(
                RequirementShotResultV1(
                    requirement_id=requirement.requirement_id,
                    beat_id=requirement.beat_id,
                    proposals=(),
                    unresolved_gaps=tuple(
                        gaps + ["proposal_budget_exhausted_requires_further_operator_review"]
                    ),
                )
            )
            continue
        for row in observed:
            item, source, index, evidence = (
                row[key] for key in ("item", "source", "index", "evidence")
            )
            source_class = (
                "owned"
                if any(ref.startswith("upload:") for ref in source.permission_evidence_refs)
                else "public_reference"
            )
            if source_class not in requirement.approved_source_classes:
                continue
            linked = requirement.requirement_id in (
                set(item.requirement_ids) | preparation_links.get(str(item.id), set())
            )
            if requirement.kind != "footage" or (not visual_only and not linked):
                continue
            for shot in evidence.shots:
                token_match = visual_only and terms <= set(shot.visual_tags)
                if requirement.kind != "footage" or not (token_match or linked):
                    continue
                scale = evidence.timebase_numerator / evidence.timebase_denominator
                start, end = shot.start_tick * scale, shot.end_tick * scale
                if (item.source_in_seconds is not None and start < item.source_in_seconds) or (
                    item.source_out_seconds is not None and end > item.source_out_seconds
                ):
                    continue
                score = (2 if token_match else 0) + int(linked)
                candidates.append(
                    (
                        score,
                        str(item.id),
                        index["index_id"],
                        shot.start_tick,
                        shot.shot_id,
                        row,
                        shot,
                        token_match,
                    )
                )
        candidates.sort(key=lambda value: (-value[0], *value[1:5]))
        shortlist = candidates[: min(5, remaining)]
        chosen = []
        for score, _, _, _, _, row, shot, token_match in shortlist:
            item, source, index, evidence = (
                row[key] for key in ("item", "source", "index", "evidence")
            )
            scale = evidence.timebase_numerator / evidence.timebase_denominator
            start, end = shot.start_tick * scale, shot.end_tick * scale
            authority = source.permission_evidence_refs
            if authority and authority[0].startswith("upload:"):
                authority = authority[:1]
            identity = {
                "scope": manifest.scope.model_dump(mode="json"),
                "requirement": requirement.requirement_id,
                "dependency": requirement.dependency_digest,
                "item": str(item.id),
                "clip": str(source.clip_id),
                "sha": source.sha256,
                "index": index["index_id"],
                "evidence": row["evidence_digest"],
                "shot": shot.shot_id,
                "start": shot.start_tick,
                "end": shot.end_tick,
                "policy": POLICY,
            }
            chosen.append(
                RequirementBindingV1(
                    binding_id="rb_" + digest(identity),
                    scope=manifest.scope,
                    requirement_id=requirement.requirement_id,
                    beat_id=requirement.beat_id,
                    media_item_id=item.id,
                    clip_id=source.clip_id,
                    clip_sha256=source.sha256,
                    index_id=uuid.UUID(index["index_id"]),
                    shot_id=shot.shot_id,
                    source_origin_tick=evidence.source_origin_tick,
                    duration_ticks=evidence.duration_ticks,
                    coverage=evidence.coverage,
                    index_policy_version=evidence.policy_version,
                    visual_tags=tuple(shot.visual_tags),
                    start_tick=shot.start_tick,
                    end_tick=shot.end_tick,
                    keyframe_tick=shot.keyframe_tick,
                    timebase_numerator=evidence.timebase_numerator,
                    timebase_denominator=evidence.timebase_denominator,
                    source_in_seconds=start,
                    source_out_seconds=end,
                    keyframe_sha256=shot.keyframe_sha256,
                    evidence_refs=(
                        f"preparation:{preparation}",
                        f"media-item:{item.id}",
                        f"scene-index:{index['index_id']}",
                        f"shot:{index['index_id']}:{shot.shot_id}",
                        f"frame-sha256:{shot.keyframe_sha256}",
                        *authority,
                    ),
                    rank_score=score,
                    match_basis="measured_visual_tokens"
                    if token_match
                    else "requirement_link_fallback",
                    match_confidence="limited_visual_only"
                    if token_match
                    else "unverified_semantics",
                    confidence_limits=LIMITS,
                    explanation=(
                        "Requested visual tokens occur in the measured start frame. "
                        if token_match
                        else "The Bin/preparation links this source to the requirement; "
                        "no observed evidence verifies the requested scene. "
                    )
                    + "The interval is a measured visual-change proposal; review before use.",
                    rights_assessment_id=item.rights_assessment_id,
                    rights_assessment_version=item.rights_assessment_version,
                    production_eligible=item.rights_status == "production_cleared",
                )
            )
        remaining -= len(chosen)
        if len(chosen) < len(candidates):
            gaps.append("additional_candidates_omitted_by_declared_proposal_budget")
        if not chosen:
            gaps.append("no_supported_measured_shot_proposal")
        if any(not value.production_eligible for value in chosen):
            gaps.append("current_production_clearance_required")
        if item_gaps:
            gaps.append("selected_sources_have_unresolved_inspection_gaps")
        results.append(
            RequirementShotResultV1(
                requirement_id=requirement.requirement_id,
                beat_id=requirement.beat_id,
                proposals=tuple(chosen),
                unresolved_gaps=tuple(gaps),
            )
        )
    return ShotProposalPackageV1(
        scope=manifest.scope,
        input_revision=revision,
        preparation_run_id=preparation,
        requirements=tuple(results),
        item_ids=tuple(
            sorted(
                {row["item"].id for row in observed} | {uuid.UUID(key) for key in item_gaps},
                key=str,
            )
        ),
        item_gaps=item_gaps,
    )


def _links(preparation: uuid.UUID) -> dict[str, set[str]]:
    with session_scope() as session:
        row = session.get(EditorialRun, preparation)
        assert row is not None
        return {
            value["media_item_id"]: set(value["requirement_ids"])
            for value in row.artifacts.get("bin_bindings", [])
        }


def match_requirements(
    channel: uuid.UUID,
    project: uuid.UUID,
    request: MatchRequirementsRequest,
    *,
    actor: str,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    manifest = _manifest(channel, project, request.preparation_run_id, request.expected_revision)
    observed, gaps, stamp = _snapshot(channel, project, request.item_ids, store=store)
    links = _links(request.preparation_run_id)
    dependency = digest(
        {
            "snapshot": stamp,
            "manifest": manifest.model_dump(mode="json"),
            "links": {key: sorted(value) for key, value in links.items()},
            "policy": POLICY,
        }
    )
    package_id = uuid.uuid5(project, f"shot-proposals:{channel}:{request.idempotency_key}")
    request_digest = digest(request.model_dump(mode="json"))
    payload = _rank(
        manifest, request.expected_revision, request.preparation_run_id, observed, gaps, links
    )
    _, _, final_stamp = _snapshot(
        channel, project, request.item_ids, store=store, verify_bytes=False
    )
    if final_stamp != stamp or _links(request.preparation_run_id) != links:
        raise EditorialConflict("Source, permission or index changed before saving proposals")
    with session_scope() as session:
        ensure_active_profile(session, channel)
        current = _lock_project(session, channel, project)
        if current.revision != request.expected_revision:
            raise EditorialConflict("Script changed during matching; saved input is preserved")
        previous = session.get(RequirementShotPackage, package_id)
        if previous:
            if (
                previous.request_digest != request_digest
                or previous.dependency_digest != dependency
            ):
                raise EditorialConflict(
                    "Proposal identity has changed inputs or evidence; use a new request"
                )
            payload = ShotProposalPackageV1.model_validate(previous.payload)
        else:
            row = RequirementShotPackage(
                id=package_id,
                channel_profile_id=channel,
                project_id=project,
                input_revision=request.expected_revision,
                request_digest=request_digest,
                dependency_digest=dependency,
                payload=payload.model_dump(mode="json"),
                actor=actor,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError as exc:
                raise EditorialConflict("Matching already saved; replay the same request") from exc
    return get_shot_package(channel, project, package_id, store=store)


def _package(channel: uuid.UUID, project: uuid.UUID, package_id: uuid.UUID) -> Any:
    with session_scope() as session:
        ensure_active_profile(session, channel)
        _lock_project(session, channel, project)
        row = session.get(RequirementShotPackage, package_id)
        if row is None or row.channel_profile_id != channel or row.project_id != project:
            raise EditorialNotFound("Shot proposal package not found in this project/channel")
        session.expunge(row)
        return row


def get_shot_package(
    channel: uuid.UUID,
    project: uuid.UUID,
    package_id: uuid.UUID,
    *,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    row = _package(channel, project, package_id)
    payload = ShotProposalPackageV1.model_validate(row.payload)
    status = "current"
    try:
        manifest = _manifest(channel, project, payload.preparation_run_id, row.input_revision)
    except EditorialConflict:
        status = "stale_revision"
        manifest = None
    if manifest is not None:
        item_ids = payload.item_ids
        observed, gaps, stamp = _snapshot(channel, project, item_ids, store=store)
        links = _links(payload.preparation_run_id)
        current_digest = digest(
            {
                "snapshot": stamp,
                "manifest": manifest.model_dump(mode="json"),
                "links": {key: sorted(value) for key, value in links.items()},
                "policy": POLICY,
            }
        )
        if current_digest != row.dependency_digest:
            status = "stale_source_or_permission"
        _manifest(channel, project, payload.preparation_run_id, row.input_revision)
    # Return latest choices relevant to this package, not unbounded historical decisions.
    with session_scope() as session:
        latest_versions = (
            select(
                OperatorShotChoice.requirement_id,
                func.max(OperatorShotChoice.version).label("version"),
            )
            .where(
                OperatorShotChoice.channel_profile_id == channel,
                OperatorShotChoice.project_id == project,
                OperatorShotChoice.requirement_id.in_(
                    [value.requirement_id for value in payload.requirements]
                ),
            )
            .group_by(OperatorShotChoice.requirement_id)
            .subquery()
        )
        choices = list(
            session.scalars(
                select(OperatorShotChoice)
                .join(
                    latest_versions,
                    and_(
                        OperatorShotChoice.requirement_id == latest_versions.c.requirement_id,
                        OperatorShotChoice.version == latest_versions.c.version,
                    ),
                )
                .where(
                    OperatorShotChoice.channel_profile_id == channel,
                    OperatorShotChoice.project_id == project,
                )
                .order_by(OperatorShotChoice.requirement_id)
            )
        )
    latest = {value.requirement_id: value for value in choices}
    result = payload.model_dump(mode="json")
    for requirement in result["requirements"]:
        for proposal in requirement["proposals"]:
            proposal["current_status"] = status
            if status != "current":
                proposal["production_eligible"] = False
    operator_choices = []
    for value in latest.values():
        choice_status = (
            status if value.payload["package_id"] == str(package_id) else "requires_revalidation"
        )
        choice_binding = {**value.payload["binding"], "current_status": choice_status}
        if choice_status != "current":
            choice_binding["production_eligible"] = False
        operator_choices.append(
            {
                **value.payload,
                "binding": choice_binding,
                "version": value.version,
                "current_status": choice_status,
            }
        )
    result.update(
        package_id=str(package_id), current_status=status, operator_choices=operator_choices
    )
    return result


def select_shot(
    channel: uuid.UUID,
    project: uuid.UUID,
    request: SelectShotRequest,
    *,
    actor: str,
    store: ObjectStore | None = None,
) -> dict[str, Any]:
    current = get_shot_package(channel, project, request.package_id, store=store)
    if (
        current["current_status"] != "current"
        or current["input_revision"] != request.expected_revision
    ):
        raise EditorialConflict("Proposal evidence is stale; recheck before selecting")
    binding = next(
        (
            value
            for result in current["requirements"]
            for value in result["proposals"]
            if value["binding_id"] == request.binding_id
        ),
        None,
    )
    if binding is None:
        raise EditorialNotFound("Measured proposal not found in this package")
    # Recheck metadata after byte verification and before persisting an operator decision.
    if (
        get_shot_package(channel, project, request.package_id, store=store)["current_status"]
        != "current"
    ):
        raise EditorialConflict("Permission or source changed before selection")
    choice_id = uuid.uuid5(project, f"shot-choice:{channel}:{request.idempotency_key}")
    request_digest = digest(request.model_dump(mode="json"))
    with session_scope() as session:
        ensure_active_profile(session, channel)
        row = _lock_project(session, channel, project)
        if row.revision != request.expected_revision:
            raise EditorialConflict("Script changed before selection")
        previous = session.get(OperatorShotChoice, choice_id)
        if previous:
            if previous.request_digest != request_digest:
                raise EditorialConflict("Choice identity was used for different input")
            return {**previous.payload, "version": previous.version}
        latest = session.scalar(
            select(OperatorShotChoice)
            .where(
                OperatorShotChoice.project_id == project,
                OperatorShotChoice.channel_profile_id == channel,
                OperatorShotChoice.requirement_id == binding["requirement_id"],
            )
            .order_by(OperatorShotChoice.version.desc())
            .limit(1)
        )
        version = latest.version if latest else 0
        if version != request.expected_choice_version:
            raise EditorialConflict(
                "Operator choice changed; reload before explicitly replacing it"
            )
        value = {
            "choice_id": str(choice_id),
            "package_id": str(request.package_id),
            "binding": binding,
            "locked": request.locked,
            "note": request.note,
            "state": "operator_selected",
            "rights_granted": False,
        }
        try:
            with session.begin_nested():
                session.add(
                    OperatorShotChoice(
                        id=choice_id,
                        channel_profile_id=channel,
                        project_id=project,
                        requirement_id=binding["requirement_id"],
                        version=version + 1,
                        request_digest=request_digest,
                        payload=value,
                        actor=actor,
                    )
                )
                session.flush()
        except IntegrityError as exc:
            raise EditorialConflict("Operator choice changed; reload before replacing it") from exc
        return {**value, "version": version + 1}
