from __future__ import annotations

from datetime import UTC, datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class YouTubeAnalyticsWorkflow:
    @workflow.run
    async def run(
        self,
        publication_id: str,
        anchor_epoch: float,
        offsets_hours: list[int],
    ) -> dict[str, object]:
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
                await workflow.execute_activity(
                    "collect_analytics_snapshot_activity",
                    args=[publication_id, f"offset-{offset}h"],
                    start_to_close_timeout=timedelta(minutes=3),
                    retry_policy=retry,
                    result_type=dict[str, object],
                )
                sampled += 1
            except Exception as exc:
                failures += 1
                await workflow.execute_activity(
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
    async def run(self, publication_id: str, sample_key: str) -> dict[str, object]:
        return await workflow.execute_activity(
            "collect_analytics_snapshot_activity",
            args=[publication_id, sample_key],
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=2),
                maximum_attempts=5,
            ),
            result_type=dict[str, object],
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
    ) -> dict[str, object]:
        local_retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=5,
        )
        provider_mutation_once = RetryPolicy(maximum_attempts=1)
        try:
            prepared = await workflow.execute_activity(
                "prepare_publication_activity",
                publication_id,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            if not prepared.get("youtube_video_id"):
                await workflow.execute_activity(
                    "initiate_upload_session_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=provider_mutation_once,
                    result_type=dict[str, object],
                )
                await workflow.execute_activity(
                    "upload_video_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(minutes=45),
                    heartbeat_timeout=timedelta(minutes=5),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )

            processing_complete = False
            for _ in range(max_polls):
                current = await workflow.execute_activity(
                    "refresh_video_status_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                if current.get("complete"):
                    processing_complete = True
                    break
                await workflow.sleep(poll_seconds)

            if not processing_complete:
                await workflow.execute_activity(
                    "mark_processing_timeout_activity",
                    publication_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                return {
                    "publication_id": publication_id,
                    "status": "processing_timeout",
                }

            finalized = await workflow.execute_activity(
                "finalize_publication_activity",
                publication_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
        except Exception as exc:
            await workflow.execute_activity(
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
            await workflow.execute_activity(
                "mark_analytics_observation_failed",
                args=[publication_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        return {
            "publication_id": publication_id,
            "status": finalized.get("status"),
        }
