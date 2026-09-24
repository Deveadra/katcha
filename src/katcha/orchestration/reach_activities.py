from __future__ import annotations

import uuid

from temporalio import activity

from katcha.integrations.youtube.reporting import (
    REACH_REPORT_TYPE,
    create_reporting_job,
    download_report,
    list_job_reports,
    list_reporting_jobs,
)
from katcha.services.reach_reporting import (
    activate_reach_job,
    ensure_local_reach_job,
    import_reach_report,
    mark_job_create_started,
)


@activity.defn
def prepare_reach_reporting_job_activity(connection_id: str) -> dict[str, object]:
    connection_uuid = uuid.UUID(connection_id)
    local = ensure_local_reach_job(connection_uuid)
    jobs = list_reporting_jobs(connection_uuid)
    matching = [
        job
        for job in jobs
        if str(job.get("reportTypeId") or "") == REACH_REPORT_TYPE
    ]
    if matching:
        provider = matching[0]
        provider_job_id = str(provider.get("id") or "")
        if not provider_job_id:
            raise RuntimeError("matching YouTube reach reporting job has no provider ID")
        active = activate_reach_job(
            local.id,
            provider_job_id=provider_job_id,
            provider_payload=provider,
        )
        return {
            "connection_id": connection_id,
            "reach_job_id": str(active.id),
            "provider_job_id": provider_job_id,
            "needs_create": False,
        }
    if local.provider_job_id:
        raise RuntimeError("stored reach job no longer exists at the provider")
    mark_job_create_started(local.id)
    return {
        "connection_id": connection_id,
        "reach_job_id": str(local.id),
        "provider_job_id": None,
        "needs_create": True,
    }


@activity.defn
def create_reach_reporting_job_activity(
    connection_id: str,
    reach_job_id: str,
) -> dict[str, object]:
    connection_uuid = uuid.UUID(connection_id)
    local_uuid = uuid.UUID(reach_job_id)
    payload = create_reporting_job(connection_uuid)
    provider_job_id = str(payload.get("id") or "")
    if not provider_job_id:
        raise RuntimeError("YouTube Reporting API create response has no job ID")
    active = activate_reach_job(
        local_uuid,
        provider_job_id=provider_job_id,
        provider_payload=payload,
    )
    return {
        "connection_id": connection_id,
        "reach_job_id": str(active.id),
        "provider_job_id": provider_job_id,
    }


@activity.defn
def sync_reach_reports_activity(
    connection_id: str,
    reach_job_id: str,
    provider_job_id: str,
) -> dict[str, object]:
    connection_uuid = uuid.UUID(connection_id)
    local_uuid = uuid.UUID(reach_job_id)
    reports = list_job_reports(connection_uuid, provider_job_id)
    imported = 0
    reused = 0
    observations = 0
    for report in sorted(reports, key=lambda item: str(item.get("createTime") or "")):
        download_url = str(report.get("downloadUrl") or "").strip()
        if not download_url:
            continue
        payload = download_report(connection_uuid, download_url)
        result = import_reach_report(
            connection_uuid,
            local_uuid,
            report=report,
            payload=payload,
        )
        if bool(result.get("imported")):
            imported += 1
        else:
            reused += 1
        observations += int(result.get("observations_created") or 0)
    return {
        "connection_id": connection_id,
        "reach_job_id": reach_job_id,
        "provider_job_id": provider_job_id,
        "reports_seen": len(reports),
        "reports_imported": imported,
        "reports_reused": reused,
        "observations_created": observations,
    }
