from __future__ import annotations

import uuid

from sqlalchemy import select

from katcha.db import session_scope
from katcha.domain import AnalysisStatus
from katcha.models import Clip, ClipAnalysisRun


def register_analysis(clip_id: uuid.UUID, *, force_retry: bool = False) -> ClipAnalysisRun:
    with session_scope() as session:
        clip = session.get(Clip, clip_id)
        if clip is None:
            raise ValueError(f"clip not found: {clip_id}")

        latest = session.scalar(
            select(ClipAnalysisRun)
            .where(ClipAnalysisRun.clip_id == clip_id)
            .order_by(ClipAnalysisRun.created_at.desc())
            .limit(1)
        )
        if latest is not None and not force_retry:
            return latest

        run_id = uuid.uuid4()
        workflow_id = f"clip-analysis-{clip_id}-{run_id}"
        run = ClipAnalysisRun(
            id=run_id,
            clip_id=clip_id,
            workflow_id=workflow_id,
            status=AnalysisStatus.QUEUED.value,
            stage="queued",
        )
        session.add(run)
        session.flush()
        session.refresh(run)
        session.expunge(run)
        return run
