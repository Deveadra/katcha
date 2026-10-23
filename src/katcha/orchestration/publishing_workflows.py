from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.orchestration.resource_queue import execute_resource_activity


@workflow.defn
class YouTubeAnalyticsWorkflow:
    @workflow.run
    async def run(
        self,
        publication_id: str,
        anchor_epoch: float,
        offsets_hours: list[int],
    ) -> dict[str, Any]:
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=10),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=10),
            maximum_attempts=5,
        )
        anchor = datetime.fromtimestamp(anchor_epoch, tz=UTC)
        sampled = 0
        failures = 0
        for offset in sorted(set(offsets_hours)):
            target = anchor + timedelta(hours=offset)
            delay = (target - workflow.now()).total_seconds()
            if delay > 0:
                await workflow.sleep(delay)
            try:
                await execute_resource_activity(
                    "collect_analytics_snapshot_activity",
                    args=[publication_id, f"offset-{offset}h"],
                    start_to_close_timeout=timedelta(minutes=3),
                    retry_policy=retry,

                )
                sampled += 1
            except Exception as exc:
                failures += 1
                await execute_resource_activity(
                    "mark_analytics_observation_failed",
                    args=[publication_id, f"offset-{offset}h: {exc}"],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
        return {
            "publication_id": publication_id,
            "sampled": sampled,
            "failures": failures,
        }


@workflow.defn
class YouTubeAnalyticsRefreshWorkflow:
    @workflow.run
    async def run(self, publication_id: str, sample_key: str) -> dict[str, Any]:
        return await execute_resource_activity(
            "collect_analytics_snapshot_activity",
            args=[publication_id, sample_key],
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=5,
            ),

        )


@workflow.defn
class YouTubePublicationWorkflow:
    @workflow.run
    async def run(
        self,
        publication_id: str,
        poll_seconds: int,
        max_polls: int,
        analytics_offsets_hours: list[int],
    ) -> dict[str, Any]:
        local_retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=5,
        )
        provider_mutation_once = RetryPolicy(maximum_attempts=1)
        try:
            prepared = await execute_resource_activity(
                "prepare_publication_activity",
                publication_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local_retry,

            )
            if not prepared.get("youtube_video_id"):
                await execute_resource_activity(
                    "initiate_upload_session_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=provider_mutation_once,

                )
                await execute_resource_activity(
                    "upload_video_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(minutes=45),
                    heartbeat_timeout=timedelta(minutes=5),
                    retry_policy=local_retry,

                )

            processing_complete = False
            for _ in range(max_polls):
                current = await execute_resource_activity(
                    "refresh_video_status_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=local_retry,

                )
                if current.get("complete"):
                    processing_complete = True
                    break
                await workflow.sleep(poll_seconds)

            if not processing_complete:
                await execute_resource_activity(
                    "mark_processing_timeout_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                return {
                    "publication_id": publication_id,
                    "status": "processing_timeout",
                }

            finalized = await execute_resource_activity(
                "finalize_publication_activity",
                publication_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,

            )
        except Exception as exc:
            await execute_resource_activity(
                "mark_publication_failed",
                args=[publication_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise

        try:
            await workflow.execute_child_workflow(
                YouTubeAnalyticsWorkflow.run,
                args=[
                    publication_id,
                    float(finalized["analytics_anchor_epoch"]),
                    analytics_offsets_hours,
                ],
                id=f"yt-analytics-{publication_id}",
            )
        except Exception as exc:
            await execute_resource_activity(
                "mark_analytics_observation_failed",
                args=[publication_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        return {
            "publication_id": publication_id,
            "status": finalized.get("status"),
        }
