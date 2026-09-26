from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE, MAX_DISCOVERY_PAGES
from katcha.orchestration.trend_workflows import ChannelTrendRefreshWorkflow
from katcha.trends.runtime import TREND_TASK_QUEUE

_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
)
_PROVIDER_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)


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
                    retry_policy=_PROVIDER_ACTIVITY_RETRY,

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

        )
        raw_runs = prepared.get("runs")
        runs = raw_runs if isinstance(raw_runs, list) else []

        async def execute_run(raw: object) -> dict[str, object]:
            item = raw if isinstance(raw, dict) else {}
            adapter_key = str(item.get("adapter_key") or "")
            action = str(item.get("action") or "execute")
            run_id = str(item.get("run_id") or "")
            if action == "reuse":
                return {
                    "run_id": run_id,
                    "adapter_key": adapter_key,
                    "success": bool(run_id),
                    "skipped": False,
                    "reused": True,
                    "reason": str(item.get("reason") or "shared_reuse"),
                }
            if action != "execute":
                return {
                    "run_id": None,
                    "adapter_key": adapter_key,
                    "success": False,
                    "skipped": True,
                    "reused": False,
                    "reason": str(item.get("reason") or "deferred"),
                }
            if not run_id:
                return {
                    "run_id": None,
                    "adapter_key": adapter_key,
                    "success": False,
                    "skipped": False,
                    "reused": False,
                    "error": "prepared discovery run is missing run_id",
                }
            child_id = str(item.get("workflow_id") or f"discovery-run-{run_id}")
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
                    "skipped": False,
                    "reused": False,
                    "result": result,
                }
            except Exception as exc:
                return {
                    "run_id": run_id,
                    "adapter_key": adapter_key,
                    "success": False,
                    "skipped": False,
                    "reused": False,
                    "error": str(exc)[:1000],
                }

        results = await asyncio.gather(*(execute_run(item) for item in runs))
        successful_run_ids = [
            str(item["run_id"])
            for item in results
            if bool(item.get("success")) and item.get("run_id")
        ]
        skipped_count = sum(1 for item in results if bool(item.get("skipped")))
        failed_count = sum(
            1
            for item in results
            if not bool(item.get("success")) and not bool(item.get("skipped"))
        )
        final = await workflow.execute_activity(
            "finalize_topic_watch_execution_activity",
            args=[topic_watch_id, execution_key, successful_run_ids, top_n],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_ACTIVITY_RETRY,

        )

        opportunity_refresh: dict[str, object] | None = None
        refresh_error: str | None = None
        channel_profile_id = str(final.get("channel_profile_id") or "")
        if final.get("status") == "completed" and channel_profile_id:
            digest = hashlib.sha256(
                f"{topic_watch_id}:{execution_key}".encode()
            ).hexdigest()[:24]
            try:
                opportunity_refresh = await workflow.execute_child_workflow(
                    ChannelTrendRefreshWorkflow.run,
                    args=[channel_profile_id, f"discovery:{execution_key}"],
                    id=f"channel-trend-from-watch-{digest}",
                    task_queue=TREND_TASK_QUEUE,
                )
            except Exception as exc:
                refresh_error = str(exc)[:1000]

        return {
            "topic_watch_id": topic_watch_id,
            "execution_key": execution_key,
            "runs": results,
            "successful_run_count": len(successful_run_ids),
            "failed_run_count": failed_count,
            "skipped_run_count": skipped_count,
            "reused_run_count": sum(1 for item in results if bool(item.get("reused"))),
            "queue": final,
            "opportunity_refresh": opportunity_refresh,
            "opportunity_refresh_error": refresh_error,
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
            with suppress(Exception):
                await workflow.execute_child_workflow(
                    TopicWatchWorkflow.run,
                    args=[topic_watch_id, execution_key, top_n],
                    id=child_id,
                    task_queue=DISCOVERY_TASK_QUEUE,
                )
            await workflow.sleep(timedelta(minutes=interval_minutes))
        workflow.continue_as_new(
            args=[
                topic_watch_id,
                interval_minutes,
                top_n,
                cycle_offset + cycles_per_history,
            ]
        )
