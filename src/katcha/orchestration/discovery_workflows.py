from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.acquisition.runtime import DISCOVERY_TASK_QUEUE, MAX_DISCOVERY_PAGES
from katcha.orchestration.trend_workflows import ChannelTrendRefreshWorkflow
from katcha.orchestration.workflows import ClipIngestWorkflow
from katcha.trends.runtime import TREND_TASK_QUEUE

_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=4,
)
_PROVIDER_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=1)
_COMMAND_CYCLE_LIFECYCLE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=10,
)

_GENERIC_FAILURE_MESSAGES = {
    "activity task failed",
    "child workflow execution failed",
    "workflow execution failed",
}


def _specific_failure_message(exc: BaseException) -> str:
    """Prefer the deepest actionable Temporal/provider message over wrappers."""
    seen: set[int] = set()
    current: BaseException | None = exc
    best = ""
    fallback = str(exc).strip()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        normalized = message.casefold()
        if message and normalized not in _GENERIC_FAILURE_MESSAGES:
            best = message
        nested = getattr(current, "cause", None)
        if not isinstance(nested, BaseException):
            nested = current.__cause__
        current = nested if isinstance(nested, BaseException) else None
    return (best or fallback or type(exc).__name__)[:8000]


def _cycle_lifecycle_detail(
    result: dict[str, object],
    *,
    child_workflow_id: str,
) -> dict[str, object]:
    raw_queue = result.get("queue")
    queue = raw_queue if isinstance(raw_queue, dict) else {}
    return {
        "cycle_workflow_id": child_workflow_id,
        "successful_run_count": int(result.get("successful_run_count") or 0),
        "failed_run_count": int(result.get("failed_run_count") or 0),
        "skipped_run_count": int(result.get("skipped_run_count") or 0),
        "reused_run_count": int(result.get("reused_run_count") or 0),
        "queue_status": str(queue.get("status") or ""),
        "queue_count": int(queue.get("queue_count") or 0),
        "opportunity_refresh_error": (
            str(result.get("opportunity_refresh_error") or "")[:1000] or None
        ),
    }


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
                args=[run_id, _specific_failure_message(exc)],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise


@workflow.defn
class CommandSourcePrepareWorkflow:
    @workflow.run
    async def run(
        self,
        run_id: str,
        ingest_task_queue: str,
    ) -> dict[str, object]:
        workflow_id = workflow.info().workflow_id
        try:
            discovery_workflow_id = f"discovery-run-{run_id}"
            discovery_result = await workflow.execute_child_workflow(
                DiscoveryRunWorkflow.run,
                run_id,
                id=discovery_workflow_id,
                task_queue=DISCOVERY_TASK_QUEUE,
            )
            prepared = await workflow.execute_activity(
                "prepare_command_discovery_candidates_activity",
                run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_ACTIVITY_RETRY,
                result_type=dict[str, object],
            )
            raw_items = prepared.get("prepared")
            items = raw_items if isinstance(raw_items, list) else []
            ingests: list[dict[str, object]] = []
            for raw in items:
                item = raw if isinstance(raw, dict) else {}
                source_id = str(item.get("source_id") or "")
                ingest_workflow_id = str(item.get("workflow_id") or "")
                clip_id = str(item.get("clip_id") or "")
                if clip_id:
                    ingests.append(
                        {
                            "source_id": source_id,
                            "workflow_id": ingest_workflow_id,
                            "success": True,
                            "reused": True,
                            "clip_id": clip_id,
                        }
                    )
                    continue
                if not source_id or not ingest_workflow_id:
                    continue
                try:
                    result = await workflow.execute_child_workflow(
                        ClipIngestWorkflow.run,
                        source_id,
                        id=ingest_workflow_id,
                        task_queue=ingest_task_queue,
                    )
                    ingests.append(
                        {
                            "source_id": source_id,
                            "workflow_id": ingest_workflow_id,
                            "success": True,
                            "result": result,
                        }
                    )
                except Exception as exc:
                    ingests.append(
                        {
                            "source_id": source_id,
                            "workflow_id": ingest_workflow_id,
                            "success": False,
                            "error": str(exc)[:1000],
                        }
                    )

            failed_ingests = sum(
                1 for item in ingests if not bool(item.get("success"))
            )
            lifecycle_state = "failed" if failed_ingests else "completed"
            await workflow.execute_activity(
                "record_command_source_prepare_lifecycle_activity",
                args=[
                    run_id,
                    workflow_id,
                    lifecycle_state,
                    {
                        "candidate_count": int(
                            discovery_result.get("candidate_count") or 0
                        ),
                        "prepared_count": len(items),
                        "ingest_count": len(ingests),
                        "failed_ingest_count": failed_ingests,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_ACTIVITY_RETRY,
            )
            return {
                "run_id": run_id,
                "discovery": discovery_result,
                "preparation": prepared,
                "ingests": ingests,
            }
        except Exception as exc:
            with suppress(Exception):
                await workflow.execute_activity(
                    "record_command_source_prepare_lifecycle_activity",
                    args=[
                        run_id,
                        workflow_id,
                        "failed",
                        {"error": _specific_failure_message(exc)[:2000]},
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_ACTIVITY_RETRY,
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
            cycle_state = "completed"
            detail: dict[str, object]
            try:
                result = await workflow.execute_child_workflow(
                    TopicWatchWorkflow.run,
                    args=[topic_watch_id, execution_key, top_n],
                    id=child_id,
                    task_queue=DISCOVERY_TASK_QUEUE,
                )
                detail = _cycle_lifecycle_detail(
                    result,
                    child_workflow_id=child_id,
                )
            except Exception as exc:
                cycle_state = "failed"
                detail = {
                    "cycle_workflow_id": child_id,
                    "error": str(exc)[:1000],
                }
            # Lifecycle telemetry must not stop a continuous source scout.
            with suppress(Exception):
                await workflow.execute_activity(
                    "record_topic_watch_command_cycle_activity",
                    args=[
                        topic_watch_id,
                        workflow_id,
                        execution_key,
                        cycle_state,
                        detail,
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_COMMAND_CYCLE_LIFECYCLE_RETRY,
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
