from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE, MAX_DISCOVERY_PAGES

_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
)


@workflow.defn
class DiscoveryRunWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict[str, object]:
        total_candidates = 0
        try:
            for page in range(MAX_DISCOVERY_PAGES):
                result = await workflow.execute_activity(
                    "execute_discovery_page_activity",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=_ACTIVITY_RETRY,
                    result_type=dict[str, object],
                )
                total_candidates += int(result.get("candidate_count") or 0)
                if bool(result.get("done")):
                    return {
                        "run_id": run_id,
                        "candidate_count": total_candidates,
                        "pages": page + 1,
                        "done": True,
                    }
            raise RuntimeError(
                f"discovery run exceeded {MAX_DISCOVERY_PAGES} pages without completion"
            )
        except Exception as exc:
            await workflow.execute_activity(
                "mark_discovery_run_failed",
                args=[run_id, str(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn
class TopicWatchWorkflow:
    @workflow.run
    async def run(
        self,
        topic_watch_id: str,
        execution_key: str,
        top_n: int,
    ) -> dict[str, object]:
        prepared = await workflow.execute_activity(
            "prepare_topic_watch_execution_activity",
            args=[topic_watch_id, execution_key],
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=_ACTIVITY_RETRY,
            result_type=dict[str, object],
        )
        raw_runs = prepared.get("runs")
        runs = raw_runs if isinstance(raw_runs, list) else []

        async def execute_run(raw: object) -> dict[str, object]:
            item = raw if isinstance(raw, dict) else {}
            run_id = str(item.get("run_id") or "")
            child_id = str(item.get("workflow_id") or f"discovery-run-{run_id}")
            adapter_key = str(item.get("adapter_key") or "")
            try:
                result = await workflow.execute_child_workflow(
                    DiscoveryRunWorkflow.run,
                    run_id,
                    id=child_id,
                    task_queue=DISCOVERY_TASK_QUEUE,
                )
                return {
                    "run_id": run_id,
                    "adapter_key": adapter_key,
                    "success": True,
                    "result": result,
                }
            except Exception as exc:
                return {
                    "run_id": run_id,
                    "adapter_key": adapter_key,
                    "success": False,
                    "error": str(exc)[:1000],
                }

        results = await asyncio.gather(*(execute_run(item) for item in runs))
        successful_run_ids = [
            str(item["run_id"])
            for item in results
            if bool(item.get("success")) and item.get("run_id")
        ]
        final = await workflow.execute_activity(
            "finalize_topic_watch_execution_activity",
            args=[topic_watch_id, execution_key, successful_run_ids, top_n],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_ACTIVITY_RETRY,
            result_type=dict[str, object],
        )
        return {
            "topic_watch_id": topic_watch_id,
            "execution_key": execution_key,
            "runs": results,
            "successful_run_count": len(successful_run_ids),
            "failed_run_count": len(results) - len(successful_run_ids),
            "queue": final,
        }


@workflow.defn
class TopicWatchScheduleWorkflow:
    @workflow.run
    async def run(
        self,
        topic_watch_id: str,
        interval_minutes: int,
        top_n: int,
        cycle_offset: int = 0,
    ) -> None:
        if interval_minutes < 5:
            raise ValueError("topic watch interval must be at least 5 minutes")
        workflow_id = workflow.info().workflow_id
        cycles_per_history = 96
        for local_cycle in range(cycles_per_history):
            cycle = cycle_offset + local_cycle
            digest = hashlib.sha256(f"{workflow_id}:{cycle}".encode()).hexdigest()[:24]
            execution_key = f"sched-{digest}"
            child_id = f"topic-watch-{topic_watch_id}-{digest}"
            try:
                await workflow.execute_child_workflow(
                    TopicWatchWorkflow.run,
                    args=[topic_watch_id, execution_key, top_n],
                    id=child_id,
                    task_queue=DISCOVERY_TASK_QUEUE,
                )
            except Exception:
                pass
            await workflow.sleep(timedelta(minutes=interval_minutes))
        workflow.continue_as_new(
            args=[
                topic_watch_id,
                interval_minutes,
                top_n,
                cycle_offset + cycles_per_history,
            ]
        )
