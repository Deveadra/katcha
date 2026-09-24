from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class ShortProductionWorkflow:
    @workflow.run
    async def run(self, production_id: str, start_stage: str = "script") -> dict[str, object]:
        paid_once = RetryPolicy(maximum_attempts=1)
        local_retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        )
        render_attempt_id: str | None = None
        try:
            if start_stage == "script":
                await workflow.execute_activity(
                    "generate_script_candidates",
                    production_id,
                    start_to_close_timeout=timedelta(minutes=3),
                    retry_policy=paid_once,
                    result_type=dict[str, object],
                )
                await workflow.execute_activity(
                    "select_script_candidate",
                    production_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                await workflow.execute_activity(
                    "generate_narration_assets",
                    production_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=paid_once,
                    result_type=dict[str, object],
                )
            elif start_stage == "voice":
                await workflow.execute_activity(
                    "generate_narration_assets",
                    production_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=paid_once,
                    result_type=dict[str, object],
                )
            elif start_stage != "render":
                raise ValueError(f"unsupported production start stage: {start_stage}")

            attempt = await workflow.execute_activity(
                "begin_render_attempt_activity",
                args=[
                    "production",
                    production_id,
                    workflow.info().workflow_id,
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            render_attempt_id = str(attempt["render_attempt_id"])
            render_generation = int(attempt["attempt_number"])

            await workflow.execute_activity(
                "build_render_manifest_activity",
                args=[production_id, render_generation],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            await workflow.execute_activity(
                "pre_render_qc_activity",
                args=["production", production_id, render_attempt_id],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            rendered = await workflow.execute_activity(
                "render_short_activity",
                args=[production_id, render_generation],
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            await workflow.execute_activity(
                "post_render_qc_activity",
                args=["production", production_id, render_attempt_id],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            return {
                "production_id": production_id,
                "status": "review",
                "output_key": rendered.get("output_key"),
                "render_attempt_id": render_attempt_id,
                "render_attempt_number": render_generation,
            }
        except Exception as exc:
            if render_attempt_id is not None:
                await workflow.execute_activity(
                    "dead_letter_render_attempt_activity",
                    args=[render_attempt_id, str(exc)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            await workflow.execute_activity(
                "mark_production_failed",
                args=[production_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
