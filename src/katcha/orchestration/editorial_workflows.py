from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class EditorialProjectWorkflow:
    """Checkpointed media intake, deliberately excluding unbudgeted AI side effects."""

    @workflow.run
    async def run(self, run_id: str, attempt: int) -> dict:
        local = RetryPolicy(maximum_attempts=3)
        # Expensive activities persist receipts; retries never restart an entire project.
        media = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=10))

        async def step(name: str, *args):
            return await workflow.execute_activity(
                name,
                args=[run_id, attempt, *args],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local,
            )

        try:
            context = await step("editorial_begin")
            if context.get("target") == "render":
                return await workflow.execute_activity(
                    "editorial_render",
                    args=[run_id, attempt],
                    start_to_close_timeout=timedelta(minutes=35),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            if context.get("target") == "assets":
                return await workflow.execute_activity(
                    "editorial_scout_assets",
                    args=[run_id, attempt],
                    start_to_close_timeout=timedelta(hours=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            if context.get("target") == "acquire_assets":
                for position in range(context["asset_count"]):
                    source = await step("editorial_prepare_asset", position)
                    if source.get("blocked"):
                        return {"editorial_run_id": run_id, "status": "blocked"}
                    if not source.get("clip_id"):
                        source = await workflow.execute_activity(
                            "ingest_source",
                            source["source_id"],
                            task_queue=context["ingest_queue"],
                            start_to_close_timeout=timedelta(minutes=20),
                            retry_policy=media,
                        )
                    await step("editorial_capture_asset", position, source["clip_id"])
                return await step("editorial_finish_assets")
            for position in range(context["source_count"]):
                source = await step("editorial_prepare_source", position)
                if not source.get("clip_id"):
                    # Use the managed ingest activity directly: the generic ingest workflow
                    # also schedules unrelated AI/passthrough work, outside this project's policy.
                    source = await workflow.execute_activity(
                        "ingest_source",
                        source["source_id"],
                        task_queue=context["ingest_queue"],
                        start_to_close_timeout=timedelta(minutes=20),
                        retry_policy=media,
                    )
                analysis = await step("editorial_prepare_analysis", position, source["clip_id"])
                if not analysis.get("complete"):
                    if not analysis["reusable"]:
                        await workflow.execute_activity(
                            "build_local_intelligence",
                            analysis["analysis_run_id"],
                            task_queue=context["analysis_queue"],
                            start_to_close_timeout=timedelta(minutes=30),
                            retry_policy=media,
                        )
                        await workflow.execute_activity(
                            "score_local_candidate",
                            analysis["analysis_run_id"],
                            task_queue=context["analysis_queue"],
                            start_to_close_timeout=timedelta(minutes=2),
                            retry_policy=local,
                        )
                    await step("editorial_capture_source", position)
            result = await step("editorial_finish_intake")
            if context.get("target", "analysis") == "script":
                return await workflow.execute_activity(
                    "editorial_research_script",
                    args=[run_id, attempt],
                    start_to_close_timeout=timedelta(hours=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            return result
        except Exception:
            await workflow.execute_activity(
                "editorial_fail",
                args=[
                    run_id,
                    attempt,
                    "Source preparation failed. Inspect the source/analysis receipt, "
                    "correct the connection or media problem, then resume this run.",
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local,
            )
            raise
