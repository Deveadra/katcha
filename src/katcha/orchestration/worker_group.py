from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Sequence

from temporalio.worker import Worker


async def run_worker_group(workers: Sequence[Worker]) -> None:
    """Run multiple Temporal workers in one process with graceful container shutdown."""
    if not workers:
        raise ValueError("worker group cannot be empty")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        async with contextlib.AsyncExitStack() as stack:
            for worker in workers:
                await stack.enter_async_context(worker)
            if installed_signals:
                await stop.wait()
            else:
                await asyncio.Future()
    finally:
        for sig in installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
