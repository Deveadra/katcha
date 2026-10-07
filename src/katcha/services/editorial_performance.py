from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from katcha.db import session_scope
from katcha.packaging_models import (
    PublicationPackagingActivation,
    PublicationPackagingVariant,
)
from katcha.publishing_models import (
    Publication,
    PublicationAnalyticsSnapshot,
    RetentionPoint,
)
from katcha.reach_models import PublicationReachObservation
from katcha.services.editorial_costs import editorial_project_usage_cost
from katcha.services.editorial_runs import get_run


def _decimal_text(value: object) -> str | None:
    if value is None:
        return None
    return str(Decimal(str(value)))


def _source_cost(session: object, publication: Publication, project_id: uuid.UUID) -> Decimal:
    frozen = (publication.treatment_metadata or {}).get("source_cost_usd_at_registration")
    if frozen is not None:
        value = Decimal(str(frozen))
        if value < 0:
            raise ValueError("Editorial publication has an invalid frozen source cost")
        return value
    return editorial_project_usage_cost(session, project_id)


def _variant(
    session: object,
    publication: Publication,
    reach: PublicationReachObservation | None,
) -> PublicationPackagingVariant | None:
    variant_id = reach.packaging_variant_id if reach is not None else None
    if variant_id is None:
        activation = session.scalar(
            select(PublicationPackagingActivation)
            .where(
                PublicationPackagingActivation.publication_id == publication.id,
                PublicationPackagingActivation.status == "applied",
            )
            .order_by(
                PublicationPackagingActivation.applied_at.desc(),
                PublicationPackagingActivation.created_at.desc(),
            )
            .limit(1)
        )
        if activation is not None:
            variant_id = activation.variant_id
    if variant_id is None:
        preupload = dict(
            (publication.treatment_metadata or {}).get("preupload_packaging") or {}
        )
        raw = preupload.get("variant_id")
        if raw:
            try:
                variant_id = uuid.UUID(str(raw))
            except ValueError:
                raise ValueError("Editorial publication has invalid packaging lineage") from None
    return session.get(PublicationPackagingVariant, variant_id) if variant_id else None


def editorial_publication_performance(
    channel_profile_id: uuid.UUID,
    project_id: uuid.UUID,
    editorial_run_id: uuid.UUID,
) -> dict[str, object]:
    """Return measured publication outcomes for one exact Editorial render."""
    run = get_run(channel_profile_id, project_id, editorial_run_id)
    with session_scope() as session:
        publication = session.scalar(
            select(Publication).where(Publication.editorial_run_id == run.id)
        )
        if publication is None:
            return {
                "measurement_state": "not_staged",
                "publication": None,
                "analytics": None,
                "reach": None,
                "retention_50": None,
                "packaging_variant": None,
                "economics": None,
            }

        analytics = session.scalar(
            select(PublicationAnalyticsSnapshot)
            .where(PublicationAnalyticsSnapshot.publication_id == publication.id)
            .order_by(PublicationAnalyticsSnapshot.sampled_at.desc())
            .limit(1)
        )
        reach = session.scalar(
            select(PublicationReachObservation)
            .where(PublicationReachObservation.publication_id == publication.id)
            .order_by(PublicationReachObservation.report_date.desc())
            .limit(1)
        )
        retention = None
        if analytics is not None:
            points = list(
                session.scalars(
                    select(RetentionPoint).where(
                        RetentionPoint.snapshot_id == analytics.id
                    )
                )
            )
            if points:
                retention = min(
                    points,
                    key=lambda item: abs(float(item.elapsed_video_time_ratio) - 0.5),
                )
        variant = _variant(session, publication, reach)
        cost = _source_cost(session, publication, run.project_id)
        revenue = (
            Decimal(analytics.estimated_revenue)
            if analytics is not None and analytics.estimated_revenue is not None
            else None
        )
        margin = revenue - cost if revenue is not None else None

        if publication.youtube_video_id is None:
            measurement_state = "not_uploaded"
        elif analytics is None and reach is None:
            measurement_state = "awaiting_analytics"
        else:
            measurement_state = "measured"

        return {
            "measurement_state": measurement_state,
            "publication": {
                "id": str(publication.id),
                "status": publication.status,
                "stage": publication.stage,
                "youtube_video_id": publication.youtube_video_id,
                "published_at": publication.published_at,
            },
            "analytics": (
                {
                    "sample_key": analytics.sample_key,
                    "sampled_at": analytics.sampled_at,
                    "period_start": analytics.period_start,
                    "period_end": analytics.period_end,
                    "views": analytics.views,
                    "engaged_views": analytics.engaged_views,
                    "estimated_minutes_watched": _decimal_text(
                        analytics.estimated_minutes_watched
                    ),
                    "average_view_duration": _decimal_text(
                        analytics.average_view_duration
                    ),
                    "average_view_percentage": _decimal_text(
                        analytics.average_view_percentage
                    ),
                    "likes": analytics.likes,
                    "comments": analytics.comments,
                    "shares": analytics.shares,
                    "subscribers_gained": analytics.subscribers_gained,
                    "subscribers_lost": analytics.subscribers_lost,
                    "estimated_revenue_usd": _decimal_text(
                        analytics.estimated_revenue
                    ),
                    "monetized_playbacks": analytics.monetized_playbacks,
                }
                if analytics is not None
                else None
            ),
            "reach": (
                {
                    "report_date": reach.report_date,
                    "impressions": reach.impressions,
                    "ctr": _decimal_text(reach.ctr),
                    "attribution_status": reach.attribution_status,
                    "packaging_variant_id": (
                        str(reach.packaging_variant_id)
                        if reach.packaging_variant_id is not None
                        else None
                    ),
                }
                if reach is not None
                else None
            ),
            "retention_50": (
                {
                    "elapsed_video_time_ratio": _decimal_text(
                        retention.elapsed_video_time_ratio
                    ),
                    "audience_watch_ratio": _decimal_text(
                        retention.audience_watch_ratio
                    ),
                    "relative_retention_performance": _decimal_text(
                        retention.relative_retention_performance
                    ),
                }
                if retention is not None
                else None
            ),
            "packaging_variant": (
                {
                    "id": str(variant.id),
                    "variant_key": variant.variant_key,
                    "version": variant.version,
                    "title": variant.title,
                    "has_thumbnail": variant.thumbnail_storage_key is not None,
                }
                if variant is not None
                else None
            ),
            "economics": {
                "source_cost_usd": _decimal_text(cost),
                "estimated_revenue_usd": _decimal_text(revenue),
                "contribution_margin_usd": _decimal_text(margin),
            },
        }
