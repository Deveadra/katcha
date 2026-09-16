from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from katcha.acquisition.runtime import MAX_DISCOVERY_PAGES


@workflow.defn
class DiscoveryRunWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict[str, object]:
        total_candidates = 0
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=4,
        )
        try:
            for page in range(MAX_DISCOVERY_PAGES):
                result = await workflow.execute_activity(
                    "execute_discovery_page_activity",
                    run_id,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=retry_policy,
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
