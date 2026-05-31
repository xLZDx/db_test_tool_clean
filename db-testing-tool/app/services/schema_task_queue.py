"""Bounded schema task queue for background schema/KB jobs."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from app.config import settings

logger = logging.getLogger(__name__)


class SchemaTaskQueueFull(RuntimeError):
    """Raised when a schema background task cannot be accepted safely."""


@dataclass(frozen=True)
class _TaskItem:
    operation_id: str
    label: str
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


_queue: asyncio.Queue[_TaskItem] | None = None
_workers: list[asyncio.Task[None]] = []
_loop: asyncio.AbstractEventLoop | None = None
_running_operation_ids: set[str] = set()
_queued_operation_ids: set[str] = set()
_lock = threading.Lock()
_worker_count_override: int | None = None
_max_queue_override: int | None = None


def _worker_count() -> int:
    configured = _worker_count_override if _worker_count_override is not None else settings.SCHEMA_TASK_WORKER_COUNT
    return max(1, int(configured))


def _max_queue_size() -> int:
    configured = _max_queue_override if _max_queue_override is not None else settings.SCHEMA_TASK_MAX_QUEUE
    return max(1, int(configured))


async def ensure_schema_task_workers() -> None:
    """Start the bounded worker pool once for the current event loop."""
    global _queue, _loop, _workers

    loop = asyncio.get_running_loop()
    with _lock:
        if _loop is not loop:
            _queue = asyncio.Queue(maxsize=_max_queue_size())
            _loop = loop
            _workers = []
            _running_operation_ids.clear()
            _queued_operation_ids.clear()

        assert _queue is not None
        alive_workers = [worker for worker in _workers if not worker.done()]
        missing = _worker_count() - len(alive_workers)
        for idx in range(missing):
            worker_no = len(alive_workers) + idx + 1
            alive_workers.append(loop.create_task(_worker_loop(worker_no), name=f"schema-task-worker-{worker_no}"))
        _workers = alive_workers


async def enqueue_schema_task(
    operation_id: str,
    label: str,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> int:
    """Queue fn() for background execution and return queued+running depth.

    The previous implementation spawned one asyncio.Task per request. That made
    a schema-analysis burst able to create unbounded work inside the API process.
    This function now uses a fixed worker pool plus a bounded queue.
    """
    await ensure_schema_task_workers()
    item = _TaskItem(operation_id=operation_id, label=label, fn=fn, args=args, kwargs=dict(kwargs))

    with _lock:
        assert _queue is not None
        if operation_id in _queued_operation_ids or operation_id in _running_operation_ids:
            raise SchemaTaskQueueFull(f"schema task already queued or running: {operation_id}")
        try:
            _queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise SchemaTaskQueueFull(
                f"schema task queue full: max_queue={_max_queue_size()} running={len(_running_operation_ids)}"
            ) from exc
        _queued_operation_ids.add(operation_id)
        depth = len(_queued_operation_ids) + len(_running_operation_ids)

    logger.info("enqueue_schema_task: queued %s (op=%s) depth=%s", label, operation_id, depth)
    return depth


async def _worker_loop(worker_no: int) -> None:
    while True:
        assert _queue is not None
        item = await _queue.get()
        with _lock:
            _queued_operation_ids.discard(item.operation_id)
            _running_operation_ids.add(item.operation_id)
        try:
            await _run_task_item(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Background schema task %s failed: %s", item.label, exc, exc_info=True)
        finally:
            with _lock:
                _running_operation_ids.discard(item.operation_id)
            _queue.task_done()


async def _run_task_item(item: _TaskItem) -> None:
    fn = item.fn
    if asyncio.iscoroutinefunction(fn):
        await fn(*item.args, **item.kwargs)
        return

    result = await asyncio.to_thread(fn, *item.args, **item.kwargs)
    if asyncio.iscoroutine(result):
        await result


def get_queue_depth() -> int:
    """Return total queued + running background schema tasks."""
    with _lock:
        return len(_queued_operation_ids) + len(_running_operation_ids)


def get_queue_health() -> dict:
    """Return health payload for the bounded background task system."""
    with _lock:
        queued = list(_queued_operation_ids)
        running = list(_running_operation_ids)
        worker_count = len([worker for worker in _workers if not worker.done()])
    return {
        "status": "ok",
        "queue_depth": len(queued) + len(running),
        "worker_count": worker_count,
        "active_workers": len(running),
        "active_operation_ids": running,
        "queued_operation_ids": queued,
        "queued_count": len(queued),
        "running_count": len(running),
        "max_queue": _max_queue_size(),
        "workers_started": worker_count > 0,
    }


async def _reset_for_tests(*, worker_count: int = 2, max_queue: int = 50) -> None:
    """Reset module globals for deterministic queue tests."""
    global _queue, _loop, _workers, _worker_count_override, _max_queue_override

    with _lock:
        workers = list(_workers)
    for worker in workers:
        worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)

    with _lock:
        _queue = None
        _loop = None
        _workers = []
        _running_operation_ids.clear()
        _queued_operation_ids.clear()
        _worker_count_override = worker_count
        _max_queue_override = max_queue
