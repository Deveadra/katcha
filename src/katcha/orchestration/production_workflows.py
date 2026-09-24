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
                requirements = await workflow.execute_activity(
                    "production_edit_requirements_activity",
                    production_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                if requirements.get("narration_mode") in {
                    "persona_voice",
                    "explanatory_voice",
                }:
                    await workflow.execute_activity(
                        "generate_narration_assets",
                        production_id,
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=paid_once,
                        result_type=dict[str, object],
                    )
            elif start_stage == "voice":
                requirements = await workflow.execute_activity(
                    "production_edit_requirements_activity",
                    production_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,
                    result_type=dict[str, object],
                )
                if requirements.get("narration_mode") in {
                    "persona_voice",
                    "explanatory_voice",
                }:
                    await workflow.execute_activity(
                        "generate_narration_assets",
                        production_id,
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=paid_once,
                        result_type=dict[str, object],
                    )
            elif start_stage != "render":
                raise ValueError(f"unsupported production start stage: {start_stage}")

            await workflow.execute_activity(
                "build_render_manifest_activity",
                production_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            await workflow.execute_activity(
                "pre_render_qc_activity",
                args=["production", production_id],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            rendered = await workflow.execute_activity(
                "render_short_activity",
                production_id,
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            await workflow.execute_activity(
                "post_render_qc_activity",
                args=["production", production_id],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=local_retry,
                result_type=dict[str, object],
            )
            return {
                "production_id": production_id,
                "status": "review",
                "output_key": rendered.get("output_key"),
            }
        except Exception as exc:
            await workflow.execute_activity(
                "mark_production_failed",
                args=[production_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
