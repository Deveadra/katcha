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

                )
                await workflow.execute_activity(
                    "select_script_candidate",
                    production_id,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,

                )
                await workflow.execute_activity(
                    "generate_narration_assets",
                    production_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=paid_once,

                )
            elif start_stage == "voice":
                await workflow.execute_activity(
                    "generate_narration_assets",
                    production_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=paid_once,

                )
            elif start_stage != "render":
                raise ValueError(f"unsupported production start stage: {start_stage}")

            await workflow.execute_activity(
                "build_render_manifest_activity",
                production_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=local_retry,

            )
            rendered = await workflow.execute_activity(
                "render_short_activity",
                production_id,
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=local_retry,

            )
        except Exception as exc:
            await workflow.execute_activity(
                "mark_production_failed",
                args=[production_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise

        automation = await workflow.execute_activity(
            "advance_production_render_automation_activity",
            production_id,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=local_retry,

        )
        publication_id = automation.get("publication_id")
        publication_workflow_id = automation.get("publication_workflow_id")
        handoff_started = False
        if publication_id and publication_workflow_id:
            try:
                await workflow.execute_activity(
                    "start_registered_publication_activity",
                    args=[str(publication_id), str(publication_workflow_id)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=local_retry,

                )
                handoff_started = True
            except Exception:
                handoff_started = False

        return {
            "production_id": production_id,
            "status": automation.get("action") or "review_required",
            "output_key": rendered.get("output_key"),
            "render_attempt_id": rendered.get("render_attempt_id"),
            "publication_id": publication_id,
            "publication_workflow_id": publication_workflow_id,
            "publication_handoff_started": handoff_started,
        }
