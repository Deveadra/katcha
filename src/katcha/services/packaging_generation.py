from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from katcha.ai.fixtures import fixture_packaging_candidates
from katcha.ai.pricing import estimate_token_cost
from katcha.ai.router import (
    ModelTarget,
    assert_ai_budget,
    record_usage,
    release_budget_reservation,
    route_for_channel,
)
from katcha.ai.schemas import PackagingCandidate, PackagingCandidateSet
from katcha.config import Settings, get_settings
from katcha.db import session_scope
from katcha.domain import AITask
from katcha.intelligence_models import ChannelProfile
from katcha.packaging_models import (
    PackagingCandidateGeneration,
    PublicationPackagingVariant,
)
from katcha.production_models import Production, ProductionScript
from katcha.publishing_models import Publication
from katcha.services.packaging import create_packaging_variant
from katcha.services.packaging_intelligence import latest_packaging_intelligence
from katcha.short_episode_models import ShortEpisode, ShortEpisodeItem, ShortEpisodeScript

_PROMPT_VERSION = "packaging-candidates-v1"
_ESTIMATED_INCREMENT_USD = Decimal("0.02")
_TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_MAX_CONTEXT_TRANSCRIPT_CHARS = 1600


class PackagingGenerationUnavailable(RuntimeError):
    pass


class AmbiguousPackagingGeneration(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PackagingGenerationResult:
    generation: PackagingCandidateGeneration
    variants: tuple[PublicationPackagingVariant, ...]


def _normalized_title(value: str) -> str:
    return _TITLE_NORMALIZE_RE.sub(" ", value.casefold()).strip()


def _candidate_key(generation_id: uuid.UUID, index: int) -> str:
    return f"ai-{generation_id.hex[:12]}-{index + 1}"


def _active_ai_features(snapshot: dict[str, Any]) -> dict[str, Any]:
    ai = snapshot.get("ai_features")
    if not isinstance(ai, dict):
        return {}
    deep = ai.get("deep")
    bulk = ai.get("bulk")
    if isinstance(deep, dict):
        return deep
    if isinstance(bulk, dict):
        return bulk
    return {}


def _source_lineage(
    session: Any,
    publication: Publication,
) -> tuple[uuid.UUID, dict[str, Any], list[str]]:
    facts: list[str] = []
    context: dict[str, Any] = {
        "publication_id": str(publication.id),
        "current_title": publication.title,
        "current_description": publication.description,
        "treatment_metadata": dict(publication.treatment_metadata or {}),
    }
    if publication.production_id is not None:
        production = session.get(Production, publication.production_id)
        if production is None or production.channel_profile_id is None:
            raise ValueError("publication production has no channel-scoped lineage")
        context.update(
            {
                "source_kind": "production",
                "source_id": str(production.id),
                "brand_key": production.brand_key,
                "brand_version": production.brand_version,
                "brand_snapshot": dict(production.brand_snapshot or {}),
                "edit_blueprint_key": production.edit_blueprint_key,
                "edit_blueprint_version": production.edit_blueprint_version,
                "analysis": dict(production.analysis_snapshot or {}),
            }
        )
        ai = _active_ai_features(dict(production.analysis_snapshot or {}))
        for key in ("event_summary", "setup", "payoff"):
            value = str(ai.get(key) or "").strip()
            if value:
                facts.append(value)
        transcript = str((production.analysis_snapshot or {}).get("transcript") or "").strip()
        if transcript:
            facts.append(transcript[:_MAX_CONTEXT_TRANSCRIPT_CHARS])
        if production.selected_script_id is not None:
            script = session.get(ProductionScript, production.selected_script_id)
            if script is not None:
                context["selected_script"] = {
                    "style": script.style,
                    "narration": script.narration,
                    "interaction_prompt": script.interaction_prompt,
                    "metadata": dict(script.script_metadata or {}),
                }
                if script.narration.strip():
                    facts.append(script.narration.strip())
                title_angle = str((script.script_metadata or {}).get("title_angle") or "").strip()
                if title_angle:
                    facts.append(title_angle)
        return production.channel_profile_id, context, facts

    if publication.short_episode_id is not None:
        episode = session.get(ShortEpisode, publication.short_episode_id)
        if episode is None:
            raise ValueError("publication short episode lineage is missing")
        context.update(
            {
                "source_kind": "short_episode",
                "source_id": str(episode.id),
                "premise": episode.premise,
                "format_key": episode.format_key,
                "format_version": episode.format_version,
                "brand_key": episode.brand_key,
                "brand_version": episode.brand_version,
                "brand_snapshot": dict(episode.brand_snapshot or {}),
                "edit_blueprint_key": episode.edit_blueprint_key,
                "edit_blueprint_version": episode.edit_blueprint_version,
                "plan_snapshot": dict(episode.plan_snapshot or {}),
            }
        )
        facts.append(episode.premise.strip())
        if episode.selected_script_id is not None:
            script = session.get(ShortEpisodeScript, episode.selected_script_id)
            if script is not None:
                context["selected_script"] = {
                    "style": script.style,
                    "payload": dict(script.script_payload or {}),
                    "narration_beats": list(script.narration_beats or []),
                }
                for beat in script.narration_beats or []:
                    text = str(beat.get("text") or "").strip()
                    if text:
                        facts.append(text)
        items = list(
            session.scalars(
                select(ShortEpisodeItem)
                .where(ShortEpisodeItem.short_episode_id == episode.id)
                .order_by(ShortEpisodeItem.position.desc())
            )
        )
        item_summaries: list[dict[str, Any]] = []
        for item in items:
            analysis = dict(item.analysis_snapshot or {})
            ai = _active_ai_features(analysis)
            summary = str(ai.get("event_summary") or "").strip()
            if summary:
                facts.append(summary)
            item_summaries.append(
                {
                    "position": item.position,
                    "role": item.role,
                    "event_summary": summary,
                }
            )
        context["items"] = item_summaries
        return episode.channel_profile_id, context, facts

    raise ValueError(
        "automated packaging generation currently supports "
        "production and short-episode publications"
    )


def compile_packaging_context(
    publication_id: uuid.UUID,
) -> tuple[uuid.UUID, dict[str, Any], str]:
    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise ValueError(f"publication not found: {publication_id}")
        profile_id, context, facts = _source_lineage(session, publication)
        profile = session.get(ChannelProfile, profile_id)
        if profile is None:
            raise ValueError("publication source references a missing channel profile")
        if profile.youtube_connection_id != publication.youtube_connection_id:
            raise ValueError("publication channel does not match source channel profile")
        variants = list(
            session.scalars(
                select(PublicationPackagingVariant)
                .where(PublicationPackagingVariant.publication_id == publication.id)
                .order_by(
                    PublicationPackagingVariant.created_at,
                    PublicationPackagingVariant.variant_key,
                )
            )
        )
        context["existing_variants"] = [
            {
                "variant_key": row.variant_key,
                "version": row.version,
                "title": row.title,
                "has_thumbnail": row.thumbnail_storage_key is not None,
            }
            for row in variants
        ]
        context["channel_profile_id"] = str(profile.id)
        context["channel_timezone"] = profile.timezone
        context["channel_metadata"] = dict(profile.profile_metadata or {})
        context["grounding_facts"] = list(dict.fromkeys(fact for fact in facts if fact))

    intelligence = latest_packaging_intelligence(profile_id)
    if intelligence is not None:
        context["latest_packaging_evidence"] = {
            "snapshot_version": intelligence.version,
            "maturity_days": intelligence.maturity_days,
            "recommendation_status": intelligence.recommendation_status,
            "recommendations": list(intelligence.recommendations or [])[:20],
            "policy_snapshot": dict(intelligence.policy_snapshot or {}),
        }
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)
    return profile_id, context, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prompt(context: dict[str, Any], *, candidate_count: int) -> str:
    payload = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    return (
        f"Prompt version: {_PROMPT_VERSION}\n"
        "You create YouTube packaging candidates for one channel-scoped publication. "
        "Generate differentiated, compelling packaging without clickbait or invented facts. "
        f"Return exactly {candidate_count} candidates. Each candidate must use a distinct "
        "variation_family and angle. The title must be <=100 characters. Descriptions must "
        "remain factual. Thumbnail briefs are creative instructions only; do not claim an "
        "image exists. supporting_facts MUST be copied verbatim from the supplied "
        "grounding_facts array and must directly support the candidate. Do not introduce "
        "named people, places, products, counts, outcomes, quotations, or claims that are not "
        "established by those grounding facts. Preserve the channel brand/persona cues in the "
        "context while keeping packaging concise and native to YouTube. Existing titles are "
        "negative examples for duplication: create meaningfully different alternatives.\n\n"
        f"CONTEXT_JSON:\n{payload}"
    )


def _record_metadata_usage(
    *,
    target: ModelTarget,
    input_tokens: int,
    output_tokens: int,
    generation_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
) -> None:
    record_usage(
        task=AITask.METADATA,
        target=target,
        input_units=input_tokens,
        output_units=output_tokens,
        cost_usd=estimate_token_cost(target, input_tokens, output_tokens),
        reference_type="packaging_generation",
        reference_id=str(generation_id),
        metadata={
            "estimated_cost": True,
            "prompt_version": _PROMPT_VERSION,
            "pricing_basis": "public_paid_rate_2026-09-15",
        },
        reservation_id=reservation_id,
    )


def _openai_generate(
    prompt: str,
    *,
    target: ModelTarget,
    settings: Settings,
    generation_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
) -> PackagingCandidateSet:
    if not settings.openai_api_key:
        raise PackagingGenerationUnavailable("OpenAI API key is not configured")
    from openai import OpenAI

    response = OpenAI(api_key=settings.openai_api_key).responses.create(
        model=target.model,
        store=False,
        reasoning={"effort": "low"},
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "packaging_candidate_set",
                "schema": PackagingCandidateSet.model_json_schema(),
                "strict": False,
            }
        },
        max_output_tokens=1800,
    )
    usage = response.usage
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    _record_metadata_usage(
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        generation_id=generation_id,
        reservation_id=reservation_id,
    )
    return PackagingCandidateSet.model_validate_json(response.output_text)


def _gemini_generate(
    prompt: str,
    *,
    target: ModelTarget,
    settings: Settings,
    generation_id: uuid.UUID,
    reservation_id: uuid.UUID | None,
) -> PackagingCandidateSet:
    if not settings.gemini_api_key:
        raise PackagingGenerationUnavailable("Gemini API key is not configured")
    from google import genai
    from google.genai import types

    response = genai.Client(api_key=settings.gemini_api_key).models.generate_content(
        model=target.model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PackagingCandidateSet,
        ),
    )
    usage = response.usage_metadata
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    _record_metadata_usage(
        target=target,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        generation_id=generation_id,
        reservation_id=reservation_id,
    )
    return PackagingCandidateSet.model_validate_json(response.text)


def _validate_candidates(
    candidate_set: PackagingCandidateSet,
    *,
    candidate_count: int,
    context: dict[str, Any],
) -> list[PackagingCandidate]:
    candidates = list(candidate_set.candidates)
    if len(candidates) != candidate_count:
        raise ValueError(
            f"provider returned {len(candidates)} candidates; "
            f"expected {candidate_count}"
        )

    allowed_facts = {str(value).strip() for value in context.get("grounding_facts") or []}
    if not allowed_facts:
        raise ValueError("packaging context has no grounding facts")
    existing_titles = {
        _normalized_title(str(row.get("title") or ""))
        for row in context.get("existing_variants") or []
    }
    existing_titles.add(_normalized_title(str(context.get("current_title") or "")))
    families: set[str] = set()
    generated_titles: set[str] = set()

    for candidate in candidates:
        family = candidate.variation_family.strip().casefold()
        if family in families:
            raise ValueError("packaging candidates must use distinct variation families")
        families.add(family)
        normalized = _normalized_title(candidate.title)
        if not normalized:
            raise ValueError("packaging candidate title normalizes to empty")
        if normalized in existing_titles or normalized in generated_titles:
            raise ValueError("packaging candidate duplicates an existing/generated title")
        generated_titles.add(normalized)
        if not candidate.supporting_facts:
            raise ValueError("packaging candidate requires supporting facts")
        for fact in candidate.supporting_facts:
            if fact.strip() not in allowed_facts:
                raise ValueError("packaging candidate cites unsupported grounding fact")
    return candidates


def _load_generation(
    publication_id: uuid.UUID,
    generation_key: str,
) -> PackagingCandidateGeneration | None:
    with session_scope() as session:
        row = session.scalar(
            select(PackagingCandidateGeneration).where(
                PackagingCandidateGeneration.publication_id == publication_id,
                PackagingCandidateGeneration.generation_key == generation_key,
            )
        )
        if row is not None:
            session.expunge(row)
        return row


def generate_packaging_candidates(
    publication_id: uuid.UUID,
    *,
    generation_key: str,
    candidate_count: int = 3,
    settings: Settings | None = None,
) -> PackagingGenerationResult:
    key = generation_key.strip()
    if not key or len(key) > 160:
        raise ValueError("generation_key must be between 1 and 160 characters")
    if candidate_count < 2 or candidate_count > 5:
        raise ValueError("candidate_count must be between 2 and 5")

    existing = _load_generation(publication_id, key)
    if existing is not None and existing.status == "completed":
        bound_count = int(
            (existing.generation_metadata or {}).get("candidate_count")
            or len(existing.candidate_payload)
        )
        if bound_count != candidate_count:
            raise ValueError("generation_key is already bound to another candidate_count")
        with session_scope() as session:
            variants = tuple(
                session.get(PublicationPackagingVariant, uuid.UUID(value))
                for value in existing.accepted_variant_ids
            )
            resolved = tuple(value for value in variants if value is not None)
            for value in resolved:
                session.expunge(value)
        return PackagingGenerationResult(existing, resolved)

    profile_id, context, context_sha = compile_packaging_context(publication_id)
    with session_scope() as session:
        row = session.scalar(
            select(PackagingCandidateGeneration)
            .where(
                PackagingCandidateGeneration.publication_id == publication_id,
                PackagingCandidateGeneration.generation_key == key,
            )
            .with_for_update()
        )
        if row is None:
            row = PackagingCandidateGeneration(
                publication_id=publication_id,
                generation_key=key,
                status="queued",
                stage="queued",
                prompt_version=_PROMPT_VERSION,
                context_sha256=context_sha,
                generation_metadata={"candidate_count": candidate_count},
            )
            session.add(row)
            session.flush()
        elif (
            int(
                (row.generation_metadata or {}).get("candidate_count")
                or candidate_count
            )
            != candidate_count
        ):
            raise ValueError("generation_key is already bound to another candidate_count")
        elif row.status in {"failed", "ambiguous"} and not row.candidate_payload:
            raise AmbiguousPackagingGeneration(
                "generation_key is terminal after a paid/ambiguous attempt; "
                "use a new generation_key"
            )
        elif (
            not row.candidate_payload
            and row.status != "completed"
            and row.context_sha256
            and row.context_sha256 != context_sha
        ):
            raise ValueError("generation_key is already bound to a different creative context")
        generation_id = row.id
        ambiguous_started = (
            row.stage == "provider_call_started"
            and not row.candidate_payload
            and row.status != "completed"
        )

    if ambiguous_started:
        with session_scope() as session:
            stuck = session.get(PackagingCandidateGeneration, generation_id)
            if stuck is not None:
                stuck.status = "ambiguous"
                stuck.stage = "provider_call_ambiguous"
                stuck.error = "provider call may have been accepted; use a new generation_key"
        raise AmbiguousPackagingGeneration(
            "provider call may have been accepted; use a new generation_key"
        )

    row = _load_generation(publication_id, key)
    if row is None:
        raise RuntimeError("packaging generation disappeared")
    if row.status == "completed":
        with session_scope() as session:
            variants = tuple(
                session.get(PublicationPackagingVariant, uuid.UUID(value))
                for value in row.accepted_variant_ids
            )
            resolved = tuple(value for value in variants if value is not None)
            for value in resolved:
                session.expunge(value)
        return PackagingGenerationResult(row, resolved)

    candidates: list[PackagingCandidate]
    if row.candidate_payload:
        candidates = [
            PackagingCandidate.model_validate(value)
            for value in row.candidate_payload
        ]
    else:
        settings = settings or get_settings()
        if settings.resolved_ai_execution_mode() == "fixture":
            target = ModelTarget("fixture", "deterministic-packaging-v1")
            candidate_set = fixture_packaging_candidates(context, candidate_count)
            candidates = _validate_candidates(
                candidate_set,
                candidate_count=candidate_count,
                context=context,
            )
            with session_scope() as session:
                ready = session.get(PackagingCandidateGeneration, row.id)
                if ready is None:
                    raise RuntimeError(
                        "packaging generation disappeared during fixture generation"
                    )
                ready.status = "running"
                ready.stage = "candidates_ready"
                ready.context_sha256 = context_sha
                ready.provider = target.provider
                ready.model = target.model
                ready.error = None
        else:
            assert_ai_budget(_ESTIMATED_INCREMENT_USD)
            decision = route_for_channel(
                AITask.METADATA,
                profile_id,
                estimated_increment_usd=_ESTIMATED_INCREMENT_USD,
                expected_value=0.65,
                reference_type="packaging_generation",
                reference_id=str(row.id),
                reservation_key=f"packaging-generation:{publication_id}:{key}",
            )
            with session_scope() as session:
                locked = session.get(PackagingCandidateGeneration, row.id)
                if locked is None:
                    raise RuntimeError(
                        "packaging generation disappeared before provider call"
                    )
                locked.status = "running"
                locked.stage = "provider_call_started"
                locked.context_sha256 = context_sha
                locked.provider = decision.route.primary.provider
                locked.model = decision.route.primary.model
                locked.error = None

            prompt = _prompt(context, candidate_count=candidate_count)
            target = decision.route.primary
            try:
                if target.provider == "openai":
                    candidate_set = _openai_generate(
                        prompt,
                        target=target,
                        settings=settings,
                        generation_id=row.id,
                        reservation_id=decision.reservation_id,
                    )
                elif target.provider == "gemini":
                    candidate_set = _gemini_generate(
                        prompt,
                        target=target,
                        settings=settings,
                        generation_id=row.id,
                        reservation_id=decision.reservation_id,
                    )
                else:
                    raise PackagingGenerationUnavailable(
                        f"unsupported metadata provider: {target.provider}"
                    )
                candidates = _validate_candidates(
                    candidate_set,
                    candidate_count=candidate_count,
                    context=context,
                )
            except (ValidationError, ValueError) as exc:
                with session_scope() as session:
                    failed = session.get(PackagingCandidateGeneration, row.id)
                    if failed is not None:
                        failed.status = "failed"
                        failed.stage = "candidate_validation_failed"
                        failed.error = str(exc)[:8000]
                raise
            except Exception as exc:
                release_budget_reservation(
                    decision.reservation_id,
                    reason=f"packaging_generation_ambiguous:{type(exc).__name__}",
                )
                with session_scope() as session:
                    failed = session.get(PackagingCandidateGeneration, row.id)
                    if failed is not None:
                        failed.status = "ambiguous"
                        failed.stage = "provider_call_ambiguous"
                        failed.error = str(exc)[:8000]
                raise AmbiguousPackagingGeneration(
                    "packaging provider call failed ambiguously; use a new generation_key"
                ) from exc

        with session_scope() as session:
            ready = session.get(PackagingCandidateGeneration, row.id)
            if ready is None:
                raise RuntimeError("packaging generation disappeared after generation")
            ready.candidate_payload = [
                candidate.model_dump(mode="json") for candidate in candidates
            ]
            ready.provider = target.provider
            ready.model = target.model
            ready.status = "running"
            ready.stage = "candidates_ready"
            ready.generation_metadata = {
                **dict(ready.generation_metadata or {}),
                "context_sha256": context_sha,
                "prompt_version": _PROMPT_VERSION,
            }

    row = _load_generation(publication_id, key)
    if row is None:
        raise RuntimeError("packaging generation disappeared before variant persistence")

    variant_ids: list[str] = []
    for index, candidate in enumerate(candidates):
        variant = create_packaging_variant(
            publication_id,
            variant_key=_candidate_key(row.id, index),
            version=1,
            title=candidate.title,
            description=candidate.description,
            created_by="katcha-ai",
            metadata={
                "source": "automated_packaging_generation",
                "generation_id": str(row.id),
                "generation_key": key,
                "candidate_index": index,
                "variation_family": candidate.variation_family,
                "angle": candidate.angle,
                "supporting_facts": list(candidate.supporting_facts),
                "thumbnail_brief": candidate.thumbnail.model_dump(mode="json"),
                "prompt_version": _PROMPT_VERSION,
                "context_sha256": context_sha,
                "provider": row.provider,
                "model": row.model,
                "channel_profile_id": str(profile_id),
                "brand_key": context.get("brand_key"),
                "brand_version": context.get("brand_version"),
                "edit_blueprint_key": context.get("edit_blueprint_key"),
                "edit_blueprint_version": context.get("edit_blueprint_version"),
            },
        )
        variant_ids.append(str(variant.id))

    with session_scope() as session:
        completed = session.get(PackagingCandidateGeneration, row.id)
        if completed is None:
            raise RuntimeError("packaging generation disappeared during completion")
        completed.accepted_variant_ids = variant_ids
        completed.status = "completed"
        completed.stage = "variants_persisted"
        completed.error = None
        completed.completed_at = datetime.now(UTC)
        session.flush()
        session.refresh(completed)
        session.expunge(completed)

    with session_scope() as session:
        variants = tuple(
            session.get(PublicationPackagingVariant, uuid.UUID(value))
            for value in variant_ids
        )
        resolved = tuple(value for value in variants if value is not None)
        for value in resolved:
            session.expunge(value)
    return PackagingGenerationResult(completed, resolved)


def list_packaging_generations(
    publication_id: uuid.UUID,
) -> list[PackagingCandidateGeneration]:
    with session_scope() as session:
        if session.get(Publication, publication_id) is None:
            raise ValueError(f"publication not found: {publication_id}")
        rows = list(
            session.scalars(
                select(PackagingCandidateGeneration)
                .where(PackagingCandidateGeneration.publication_id == publication_id)
                .order_by(PackagingCandidateGeneration.created_at.desc())
            )
        )
        for row in rows:
            session.expunge(row)
        return rows
