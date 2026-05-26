"""Per-space concurrency primitives shared by every workflow that mutates a space.

Ingest, lint, and (future) reindex must not run concurrently on the same space;
they all read and write the same bucket and DB rows. One ``asyncio.Lock`` per
space serialises them in-process.

For multi-worker deployments a distributed lock (Redis, etc.) needs to replace
``lock_for_space``; everything else above this layer stays the same.
"""

import asyncio

_space_locks: dict[str, asyncio.Lock] = {}
_background_tasks: set[asyncio.Task] = set()


def lock_for_space(space_id: str) -> asyncio.Lock:
    lock = _space_locks.get(space_id)
    if lock is None:
        lock = asyncio.Lock()
        _space_locks[space_id] = lock
    return lock


def track_background_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
