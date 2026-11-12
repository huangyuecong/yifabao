import json
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from content_service.db import Base
from content_service.job_leases import claim_job, complete_job, enqueue_job
from content_service.models import Job


@pytest.mark.parametrize("worker_count", [1, 4, 8])
def test_content_dispatcher_respects_concurrency_configuration(monkeypatch, worker_count):
    from content_service.services import create_task_dispatcher

    monkeypatch.setenv("CONTENT_JOB_MAX_WORKERS", str(worker_count))
    monkeypatch.setenv("CONTENT_JOB_MAX_QUEUE", "3")
    monkeypatch.setenv("CONTENT_JOB_TIMEOUT_SECONDS", "60")
    dispatcher = create_task_dispatcher()
    try:
        assert dispatcher.max_workers == worker_count
        assert dispatcher.max_queue_size == 3
        assert dispatcher.timeout_seconds == 60
    finally:
        dispatcher.close()


def test_default_content_dispatcher_runs_four_durable_jobs_and_queues_the_fifth(
    monkeypatch,
    tmp_path,
):
    from content_service.services import create_task_dispatcher

    monkeypatch.delenv("CONTENT_JOB_MAX_WORKERS", raising=False)
    monkeypatch.delenv("CONTENT_JOB_MAX_QUEUE", raising=False)
    monkeypatch.delenv("CONTENT_JOB_TIMEOUT_SECONDS", raising=False)
    dispatcher = create_task_dispatcher()
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-jobs.db'}")
    session_factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    all_claimed = Barrier(5)
    release_workers = Event()
    try:
        assert dispatcher.max_workers == 4
        assert dispatcher.max_queue_size == 1000
        Base.metadata.create_all(engine)
        with session_factory() as session:
            job_ids = {
                enqueue_job(
                    session,
                    task_id=f"task-{index}",
                    owner_id=f"owner-{index}",
                    kind="video_render",
                    idempotency_key=f"concurrent-{index}",
                    available_at=now,
                ).job.id
                for index in range(5)
            }
            session.commit()

        def run_job(index, wait_for_peers):
            with session_factory() as session:
                lease = claim_job(session, worker_id=f"worker-{index}", now=now)
                assert lease is not None
                session.commit()
            if wait_for_peers:
                all_claimed.wait(timeout=10)
            assert release_workers.wait(timeout=10)
            with session_factory() as session:
                completed = complete_job(
                    session,
                    lease,
                    result_json={"worker_id": f"worker-{index}"},
                    now=now + timedelta(seconds=1),
                )
                assert completed is not None and completed.status == "succeeded"
                session.commit()
            return lease.job_id

        running = [
            dispatcher.submit(lambda index=index: run_job(index, True)) for index in range(4)
        ]
        all_claimed.wait(timeout=10)
        queued = dispatcher.submit(lambda: run_job(4, False))
        assert all(future.running() for future in running)
        assert queued.running() is False
        with session_factory() as session:
            jobs = list(session.scalars(select(Job)))
            assert sum(job.status == "running" for job in jobs) == 4
            assert sum(job.status == "queued" for job in jobs) == 1
            assert len({job.lease_owner for job in jobs if job.status == "running"}) == 4

        release_workers.set()
        completed_ids = [future.result(timeout=10) for future in [*running, queued]]
        assert len(set(completed_ids)) == 5
        assert set(completed_ids) == job_ids
        with session_factory() as session:
            jobs = list(session.scalars(select(Job)))
            assert all(job.status == "succeeded" and job.attempt == 1 for job in jobs)
            assert {json.loads(job.result_json)["worker_id"] for job in jobs} == {
                f"worker-{index}" for index in range(5)
            }
    finally:
        release_workers.set()
        dispatcher.close()
        dispatcher.executor.shutdown(wait=True)
        engine.dispose()
