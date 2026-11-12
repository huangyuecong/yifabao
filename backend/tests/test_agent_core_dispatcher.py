import time
from threading import Barrier, Event

import pytest

from agent_core.tasks import ThreadTaskDispatcher


def test_dispatcher_bounds_pending_work_and_releases_capacity():
    dispatcher = ThreadTaskDispatcher(max_workers=1, max_queue_size=1)
    gate = dispatcher.submit(lambda: time.sleep(0.15))
    queued = dispatcher.submit(lambda: None)
    with pytest.raises(RuntimeError, match="任务队列已满"):
        dispatcher.submit(lambda: None)
    gate.result(timeout=2)
    queued.result(timeout=2)
    assert dispatcher.pending_count == 0
    dispatcher.close()


def test_dispatcher_rejects_submit_after_close():
    dispatcher = ThreadTaskDispatcher(max_workers=1, max_queue_size=1)
    dispatcher.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        dispatcher.submit(lambda: None)


def test_recovery_deduplicates_running_and_queued_jobs_but_allows_later_retry():
    dispatcher = ThreadTaskDispatcher(max_workers=1, max_queue_size=1)
    running = Event()
    release = Event()
    calls = []

    def work():
        running.set()
        assert release.wait(5)
        calls.append("first")

    try:
        first = dispatcher.submit(work, job_key="running-job")
        assert running.wait(5)
        second = dispatcher.submit(lambda: calls.append("queued"), job_key="queued-job")
        for _ in range(20):
            assert (
                dispatcher.submit(lambda: pytest.fail("duplicate"), job_key="running-job") is first
            )
            assert (
                dispatcher.submit(lambda: pytest.fail("duplicate"), job_key="queued-job") is second
            )
        assert dispatcher.pending_count == 2
        release.set()
        first.result(5)
        second.result(5)
        retry = dispatcher.submit(lambda: calls.append("retry"), job_key="running-job")
        retry.result(5)
        assert calls == ["first", "queued", "retry"]
    finally:
        release.set()
        dispatcher.close()
        dispatcher.executor.shutdown(wait=True)


def test_dispatcher_cancels_pending_work_after_timeout():
    dispatcher = ThreadTaskDispatcher(max_workers=1, max_queue_size=2)
    running = dispatcher.submit(lambda: time.sleep(0.2))
    pending = dispatcher.submit(lambda: None, timeout_seconds=0.02)
    time.sleep(0.05)
    assert pending.cancelled() is True
    running.result(timeout=2)
    dispatcher.close()


def test_four_workers_run_together_and_queued_cancellation_releases_capacity():
    dispatcher = ThreadTaskDispatcher(max_workers=4, max_queue_size=1)
    all_running = Barrier(5)
    release_workers = Event()
    cancelled_started = Event()
    replacement_started = Event()

    def blocked_work():
        all_running.wait(timeout=10)
        assert release_workers.wait(timeout=10)

    try:
        running = [dispatcher.submit(blocked_work) for _ in range(4)]
        all_running.wait(timeout=10)
        assert all(future.running() for future in running)

        queued = dispatcher.submit(cancelled_started.set)
        assert queued.running() is False
        assert cancelled_started.is_set() is False
        assert dispatcher.pending_count == 5
        with pytest.raises(RuntimeError, match="任务队列已满"):
            dispatcher.submit(lambda: None)

        assert queued.cancel() is True
        assert dispatcher.pending_count == 4
        replacement = dispatcher.submit(replacement_started.set)
        assert replacement.running() is False
        assert replacement_started.is_set() is False
        with pytest.raises(RuntimeError, match="任务队列已满"):
            dispatcher.submit(lambda: None)

        release_workers.set()
        for future in running:
            future.result(timeout=10)
        replacement.result(timeout=10)
        assert replacement_started.is_set() is True
        assert cancelled_started.is_set() is False
    finally:
        release_workers.set()
    dispatcher.close()
    dispatcher.executor.shutdown(wait=True)
    assert dispatcher.pending_count == 0


def test_dispatcher_rejects_invalid_limits():
    with pytest.raises(ValueError, match="max_workers"):
        ThreadTaskDispatcher(max_workers=0)
    with pytest.raises(ValueError, match="max_queue_size"):
        ThreadTaskDispatcher(max_workers=1, max_queue_size=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        ThreadTaskDispatcher(max_workers=1, timeout_seconds=0)
    dispatcher = ThreadTaskDispatcher(max_workers=1, max_queue_size=1)
    with pytest.raises(ValueError, match="timeout_seconds"):
        dispatcher.submit(lambda: None, timeout_seconds=0)
    dispatcher.close()


def test_waiting_close_finishes_running_and_queued_work_before_returning(monkeypatch):
    """The shutdown caller may release file/cache locks only after work ends."""
    from threading import Thread

    dispatcher = ThreadTaskDispatcher(max_workers=1, max_queue_size=1)
    running = Event()
    release = Event()
    shutdown_entered = Event()
    close_finished = Event()
    order = []
    original_shutdown = dispatcher.executor.shutdown

    def blocked_work():
        running.set()
        assert release.wait(5)
        order.append("running-finished")

    def observed_shutdown(*args, **kwargs):
        shutdown_entered.set()
        return original_shutdown(*args, **kwargs)

    def close_and_release_cache():
        dispatcher.close(wait=True)
        order.append("cache-lock-released")
        close_finished.set()

    monkeypatch.setattr(dispatcher.executor, "shutdown", observed_shutdown)
    first = dispatcher.submit(blocked_work)
    assert running.wait(5)
    second = dispatcher.submit(lambda: order.append("queued-finished"))
    closer = Thread(target=close_and_release_cache)
    closer.start()
    try:
        assert shutdown_entered.wait(5)
        assert not close_finished.is_set()
        with pytest.raises(RuntimeError, match="已关闭"):
            dispatcher.submit(lambda: None)
        release.set()
        closer.join(timeout=5)
        assert not closer.is_alive()
        assert close_finished.is_set()
        first.result(timeout=1)
        second.result(timeout=1)
        assert order == ["running-finished", "queued-finished", "cache-lock-released"]
        assert dispatcher.pending_count == 0
    finally:
        release.set()
        closer.join(timeout=5)
        original_shutdown(wait=True)
