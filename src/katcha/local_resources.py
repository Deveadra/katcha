"""Host-local exclusion shared by containers through one mounted lock file.

The caller owns durable queuing. Never wait here while holding a provider lease
or burning a Temporal activity timeout. The kernel releases the lock on exit.
"""

import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ResourceBusy(Exception):
    """No provider or storage side effect has started; leave work queued."""


@contextmanager
def resource_slot(path: Path | None, *, ticket: str | None = None) -> Iterator[None]:
    if path is None:
        yield
        return
    import fcntl

    # Do not create the directory: a missing shared mount must fail closed.
    # Never unlink this file, which could create a second independent lock inode.
    with path.open("a") as lock:
        # Waiting callers refresh a stable ticket every pass. Cancelled/dead
        # callers expire after two minutes; live callers retain FIFO position.
        # The separate queue lock is held only for this tiny admission section.
        with path.with_suffix(path.suffix + ".queue").open("a") as queue_lock:
            fcntl.flock(queue_lock, fcntl.LOCK_EX)
            waiter = None
            if ticket is not None:
                digest = hashlib.sha256(ticket.encode()).hexdigest()
                waiter = path.parent / f"{path.name}.{digest}.waiter"
                now = time.time()
                for candidate in path.parent.glob(f"{path.name}.*.waiter"):
                    try:
                        created = int(candidate.read_text())
                    except ValueError:
                        # A process may die between creating and writing its
                        # ticket. This is admission metadata, never job state.
                        candidate.unlink()
                        continue
                    if created <= 0 or now - candidate.stat().st_mtime > 120:
                        candidate.unlink()
                if not waiter.exists():
                    waiter.write_text(str(time.time_ns()))
                waiter.touch()
                first = min(
                    path.parent.glob(f"{path.name}.*.waiter"),
                    key=lambda candidate: (int(candidate.read_text()), candidate.name),
                )
                if first != waiter:
                    raise ResourceBusy
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ResourceBusy from None
            if waiter is not None:
                waiter.unlink()
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
