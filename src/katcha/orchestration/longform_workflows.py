from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.orchestration.resource_queue import execute_resource_activity


@workflow.defn
class LongformCompilationWorkflow:
    @workflow.run
    async def run(self, compilation_id: str, start_stage: str = "select") -> dict[str, Any]:
        paid_once = RetryPolicy(maximum_attempts=1)
        local_retry = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=3,
        )
        try:
            if start_stage == "select":
                await execute_resource_activity(
                    "select_compilation_candidates_activity",
                    compilation_id,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=local_retry,

                )
                start_stage = "plan"

            if start_stage == "plan":
                await execute_resource_activity(
                    "generate_longform_editor_plan_activity",
                    compilation_id,
                    start_to_close_timeout=timedelta(minutes=8),
                    retry_policy=paid_once,

                )
                await execute_resource_activity(
                    "critique_longform_plan_activity",
                    compilation_id,
                    start_to_close_timeout=timedelta(minutes=8),
                    retry_policy=paid_once,

                )
                await execute_resource_activity(
                    "finalize_longform_plan_activity",
                    compilation_id,
                    start_to_close_timeout=timedelta(minutes=8),
                    retry_policy=paid_once,

                )
                start_stage = "voice"

            if start_stage == "voice":
                await execute_resource_activity(
                    "generate_longform_narration_activity",
                    compilation_id,
                    start_to_close_timeout=timedelta(minutes=20),
                    retry_policy=paid_once,

                )
                start_stage = "render"

            if start_stage != "render":
                raise ValueError(f"unsupported long-form start stage: {start_stage}")

            await execute_resource_activity(
                "build_longform_manifest_activity",
                compilation_id,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=local_retry,

            )
            rendered = await execute_resource_activity(
                "render_longform_activity",
                compilation_id,
                start_to_close_timeout=timedelta(minutes=60),
                retry_policy=local_retry,

            )
            return {
                "compilation_id": compilation_id,
                "status": "review",
                "output_key": rendered.get("output_key"),
                "duration_seconds": rendered.get("duration_seconds"),
            }
        except Exception as exc:
            await execute_resource_activity(
                "mark_compilation_failed",
                args=[compilation_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
