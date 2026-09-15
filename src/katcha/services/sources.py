from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import SourceStatus
from katcha.integrations.download import canonicalize_url, detect_platform
from katcha.models import SourceItem


def base_workflow_id_for_url(url: str) -> str:
    canonical = canonicalize_url(url)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"ingest-{digest}"


def register_source(url: str, *, force_retry: bool = False) -> SourceItem:
    canonical = canonicalize_url(url)
    with session_scope() as session:
        existing = session.scalar(select(SourceItem).where(SourceItem.source_url == canonical))
        if existing is not None:
            if force_retry and existing.status == SourceStatus.FAILED.value:
                existing.status = SourceStatus.REGISTERED.value
                existing.error = None
                existing.workflow_id = (
                    f"{base_workflow_id_for_url(canonical)}-{uuid.uuid4().hex[:8]}"
                )
                session.flush()
                session.refresh(existing)
            return existing

        source = SourceItem(
            source_url=canonical,
            canonical_url=canonical,
            platform=detect_platform(canonical),
            status=SourceStatus.REGISTERED.value,
            workflow_id=base_workflow_id_for_url(canonical),
        )
        session.add(source)
        session.flush()
        session.refresh(source)
        return source
