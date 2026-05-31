import asyncio

import pytest


def test_schema_task_queue_worker_limit_serializes_jobs():
    asyncio.run(_test_schema_task_queue_worker_limit_serializes_jobs())


async def _test_schema_task_queue_worker_limit_serializes_jobs():
    from app.services import schema_task_queue as q

    await q._reset_for_tests(worker_count=1, max_queue=3)
    started: list[str] = []
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    async def first_job():
        started.append("op1")
        await release_first.wait()

    async def second_job():
        started.append("op2")
        await release_second.wait()

    try:
        assert await q.enqueue_schema_task("op1", "first", first_job) == 1
        await asyncio.wait_for(_wait_until(lambda: "op1" in started), timeout=1)
        assert await q.enqueue_schema_task("op2", "second", second_job) == 2
        await asyncio.sleep(0.05)

        health = q.get_queue_health()
        assert health["running_count"] == 1
        assert health["queued_count"] == 1
        assert health["active_operation_ids"] == ["op1"]
        assert health["queued_operation_ids"] == ["op2"]
        assert started == ["op1"]

        release_first.set()
        await asyncio.wait_for(_wait_until(lambda: "op2" in started), timeout=1)
    finally:
        release_first.set()
        release_second.set()
        await q._reset_for_tests()


def test_schema_task_queue_rejects_when_bounded_queue_full():
    asyncio.run(_test_schema_task_queue_rejects_when_bounded_queue_full())


async def _test_schema_task_queue_rejects_when_bounded_queue_full():
    from app.services import schema_task_queue as q

    await q._reset_for_tests(worker_count=1, max_queue=1)
    release = asyncio.Event()

    async def slow_job():
        await release.wait()

    try:
        await q.enqueue_schema_task("op1", "first", slow_job)
        await asyncio.sleep(0.05)
        await q.enqueue_schema_task("op2", "queued", slow_job)

        with pytest.raises(q.SchemaTaskQueueFull):
            await q.enqueue_schema_task("op3", "overflow", slow_job)
    finally:
        release.set()
        await q._reset_for_tests()


def test_schema_task_queue_rejects_duplicate_operation_id():
    asyncio.run(_test_schema_task_queue_rejects_duplicate_operation_id())


async def _test_schema_task_queue_rejects_duplicate_operation_id():
    from app.services import schema_task_queue as q

    await q._reset_for_tests(worker_count=1, max_queue=3)
    release = asyncio.Event()

    async def slow_job():
        await release.wait()

    try:
        await q.enqueue_schema_task("same-op", "first", slow_job)
        with pytest.raises(q.SchemaTaskQueueFull):
            await q.enqueue_schema_task("same-op", "duplicate", slow_job)
    finally:
        release.set()
        await q._reset_for_tests()


def test_schema_task_queue_runs_sync_callables_off_event_loop():
    asyncio.run(_test_schema_task_queue_runs_sync_callables_off_event_loop())


async def _test_schema_task_queue_runs_sync_callables_off_event_loop():
    from app.services import schema_task_queue as q

    await q._reset_for_tests(worker_count=1, max_queue=2)
    done = asyncio.Event()

    def sync_job():
        done.set()

    try:
        await q.enqueue_schema_task("sync-op", "sync", sync_job)
        await asyncio.wait_for(done.wait(), timeout=1)
    finally:
        await q._reset_for_tests()


async def _wait_until(predicate):
    while not predicate():
        await asyncio.sleep(0.01)
