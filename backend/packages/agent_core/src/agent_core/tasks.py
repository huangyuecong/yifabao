from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from threading import Lock, Timer


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ThreadTaskDispatcher:
    """Small durable-job dispatcher used by the development services.

    The database job/lease row remains the source of truth.  This class only
    limits local admission to avoid submitting an unbounded number of work
    items to ``ThreadPoolExecutor``.  ``timeout_seconds`` is cooperative: a
    pending future is cancelled when its deadline expires; running functions
    must still honour their database lease/cancellation checks.
    """

    def __init__(
        self,
        max_workers: int = 2,
        *,
        max_queue_size: int = 1000,
        timeout_seconds: float | None = None,
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be >= 1")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        self.timeout_seconds = timeout_seconds
        # ``max_queue_size`` limits waiting work; running workers consume
        # separate slots so one worker plus one queued item is valid.
        self._capacity = max_workers + max_queue_size
        self._slots = self._capacity
        self._lock = Lock()
        self._closed = False
        self._keyed_futures: dict[str, Future[None]] = {}

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._capacity - self._slots

    def submit(
        self,
        fn: Callable[[], None],
        *,
        timeout_seconds: float | None = None,
        job_key: str | None = None,
    ) -> Future[None]:
        """Submit work if capacity is available and return its Future.

        ``RuntimeError`` is raised when the bounded queue is full.  Callers
        that persist a database Job should leave it queued and let the normal
        recovery/dispatcher loop try again later.
        """
        deadline = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if deadline is not None and deadline <= 0:
            raise ValueError("timeout_seconds must be > 0")
        with self._lock:
            if self._closed:
                raise RuntimeError("任务调度器已关闭")
            if job_key is not None:
                existing = self._keyed_futures.get(job_key)
                if existing is not None and not existing.done():
                    return existing
            if self._slots <= 0:
                raise RuntimeError("任务队列已满")
            self._slots -= 1
            try:
                future = self.executor.submit(fn)
                if job_key is not None:
                    self._keyed_futures[job_key] = future
            except BaseException:
                self._slots += 1
                raise

        def release(_future: Future[None]) -> None:
            with self._lock:
                self._slots = min(self._capacity, self._slots + 1)
                if job_key is not None and self._keyed_futures.get(job_key) is _future:
                    self._keyed_futures.pop(job_key, None)

        future.add_done_callback(release)
        if deadline is not None:
            timer = Timer(deadline, future.cancel)
            timer.daemon = True
            timer.start()
            future.add_done_callback(lambda _future: timer.cancel())
        return future

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
        self.executor.shutdown(wait=wait, cancel_futures=False)
