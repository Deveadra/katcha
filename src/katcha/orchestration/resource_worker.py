"""Register queued variants while retaining original activities for old histories."""

from functools import wraps
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from katcha.config import get_settings
from katcha.local_resources import ResourceBusy, resource_slot
from katcha.orchestration.resource_queue import MEDIA_ACTIVITIES, PREFIX, RESOURCE_BUSY


def queued_activity(fn):
    definition = activity._Definition.must_from_callable(fn)
    if definition.is_async:
        raise ValueError("Media resource activities must be synchronous")

    @wraps(fn, updated=())
    def run(*args, **kwargs):
        settings = get_settings()
        path = settings.local_resource_lock_path if settings.local_degraded_mode else None
        if settings.local_degraded_mode and path is None:
            raise ApplicationError(
                "Single-host mode requires a shared resource lock mount",
                type="LocalMediaResourceConfiguration", non_retryable=True,
            )
        try:
            identity = (
                f"{activity.info().workflow_run_id}:{definition.name}:{args!r}:{kwargs!r}"
                if path is not None else None
            )
            with resource_slot(path, ticket=identity):
                return fn(*args, **kwargs)
        except ResourceBusy:
            raise ApplicationError(
                "Waiting for another media task to finish",
                type=RESOURCE_BUSY, non_retryable=True,
            ) from None

    return activity.defn(name=PREFIX + definition.name)(run)


class ResourceWorker(Worker):
    def __init__(self, client, **kwargs: Any):
        settings = get_settings()
        if settings.local_degraded_mode:
            path = settings.local_resource_lock_path
            if path is None or not path.parent.is_dir():
                raise ValueError("Single-host workers require the shared resource lock mount")
            kwargs["max_concurrent_activities"] = 1
        activities = list(kwargs.get("activities", []))
        kwargs["activities"] = activities + [
            queued_activity(fn) for fn in activities
            if activity._Definition.must_from_callable(fn).name in MEDIA_ACTIVITIES
        ]
        super().__init__(client, **kwargs)
