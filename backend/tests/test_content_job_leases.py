import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from content_service.db import Base
from content_service.job_leases import (
    cancel_job,
    claim_job,
    complete_job,
    enqueue_job,
    fail_job,
    heartbeat_job,
    recover_jobs,
    retry_job,
)
from content_service.models import Job

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


@pytest.fixture
def job_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        engine.dispose()


def enqueue(session, *, task_id="task-a", key=None, max_attempts=3, now=NOW):
    return enqueue_job(
        session,
        task_id=task_id,
        owner_id="owner-a",
        kind="video_render",
        result_json={"project_id": "project-a"},
        idempotency_key=key,
        max_attempts=max_attempts,
        available_at=now,
    )


def test_legacy_job_construction_keeps_defaults_and_enqueue_is_idempotent(
    job_database,
):
    with job_database() as session:
        legacy = Job(
            task_id="legacy-task",
            owner_id="owner-a",
            kind="video_composition",
        )
        session.add(legacy)
        session.flush()
        assert legacy.status == "queued"
        assert legacy.attempt == 0
        assert legacy.max_attempts == 3
        assert legacy.available_at is not None

        first = enqueue(session, key="render:revision-7")
        repeated = enqueue(session, key="render:revision-7")
        other_task = enqueue(
            session,
            task_id="task-b",
            key="render:revision-7",
        )
        no_key_a = enqueue(session)
        no_key_b = enqueue(session)
        session.commit()

        assert first.created is True
        assert repeated.created is False
        assert repeated.job.id == first.job.id
        assert other_task.job.id != first.job.id
        assert no_key_a.job.id != no_key_b.job.id

    with job_database() as session:
        after_restart = enqueue(session, key="render:revision-7")
        assert after_restart.created is False
        assert after_restart.job.id == first.job.id


def test_expired_lease_can_be_reclaimed_and_stale_worker_is_fenced(
    job_database,
):
    with job_database() as session:
        job_id = enqueue(session, key="fencing", max_attempts=2).job.id
        first = claim_job(
            session,
            worker_id="worker-a",
            job_id=job_id,
            lease_seconds=10,
            now=NOW,
        )
        assert first is not None and first.attempt == 1
        session.commit()

    with job_database() as session:
        assert (
            claim_job(
                session,
                worker_id="worker-b",
                job_id=job_id,
                now=NOW + timedelta(seconds=5),
            )
            is None
        )
        second = claim_job(
            session,
            worker_id="worker-b",
            job_id=job_id,
            lease_seconds=20,
            now=NOW + timedelta(seconds=11),
        )
        assert second is not None
        assert second.attempt == 2
        assert second.token != first.token
        session.commit()

    with job_database() as session:
        assert (
            complete_job(
                session,
                first,
                result_json={"storage_key": "stale.mp4"},
                now=NOW + timedelta(seconds=12),
            )
            is None
        )
        heartbeat = heartbeat_job(
            session,
            second,
            progress=40,
            lease_seconds=20,
            now=NOW + timedelta(seconds=12),
        )
        assert heartbeat.renewed is True
        lower_progress = heartbeat_job(
            session,
            second,
            progress=20,
            lease_seconds=20,
            now=NOW + timedelta(seconds=13),
        )
        assert lower_progress.renewed is True
        completed = complete_job(
            session,
            second,
            result_json={"storage_key": "final.mp4"},
            now=NOW + timedelta(seconds=14),
        )
        assert completed is not None and completed.status == "succeeded"
        session.commit()

    with job_database() as session:
        stored = session.get(Job, job_id)
        assert stored.status == "succeeded"
        assert stored.progress == 100
        assert stored.attempt == 2
        assert stored.lease_owner is None
        assert json.loads(stored.result_json) == {"storage_key": "final.mp4"}


def test_sqlite_concurrent_claim_uses_one_compare_and_set_winner(job_database):
    with job_database() as session:
        job_id = enqueue(session, key="concurrent-claim").job.id
        session.commit()

    barrier = Barrier(2)

    def competing_claim(worker_id):
        with job_database() as session:
            barrier.wait(timeout=5)
            lease = claim_job(
                session,
                worker_id=worker_id,
                job_id=job_id,
                now=NOW,
            )
            session.commit()
            return lease

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(competing_claim, ("concurrent-worker-a", "concurrent-worker-b")))
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert winners[0].attempt == 1

    with job_database() as session:
        stored = session.get(Job, job_id)
        assert stored.status == "running"
        assert stored.attempt == 1
        assert stored.lease_token == winners[0].token


def test_recovery_requeues_restart_work_and_terminalizes_expired_jobs(
    job_database,
):
    expired = NOW - timedelta(seconds=1)
    live = NOW + timedelta(minutes=5)
    jobs = [
        Job(
            task_id="recoverable",
            owner_id="owner-a",
            kind="video_render",
            status="running",
            attempt=1,
            max_attempts=3,
            lease_owner="dead-worker",
            lease_token="dead-token",
            lease_expires_at=expired,
            available_at=NOW - timedelta(minutes=1),
        ),
        Job(
            task_id="unleased-legacy",
            owner_id="owner-a",
            kind="video_render",
            status="running",
            attempt=0,
            max_attempts=3,
            available_at=NOW - timedelta(minutes=1),
        ),
        Job(
            task_id="exhausted",
            owner_id="owner-a",
            kind="video_render",
            status="running",
            attempt=3,
            max_attempts=3,
            lease_owner="dead-worker",
            lease_token="dead-exhausted",
            lease_expires_at=expired,
            available_at=NOW - timedelta(minutes=1),
        ),
        Job(
            task_id="cancelled-on-recovery",
            owner_id="owner-a",
            kind="video_render",
            status="running",
            attempt=1,
            max_attempts=3,
            lease_owner="dead-worker",
            lease_token="dead-cancelled",
            lease_expires_at=expired,
            cancel_requested_at=NOW - timedelta(seconds=2),
            available_at=NOW - timedelta(minutes=1),
        ),
        Job(
            task_id="live",
            owner_id="owner-a",
            kind="video_render",
            status="running",
            attempt=1,
            max_attempts=3,
            lease_owner="live-worker",
            lease_token="live-token",
            lease_expires_at=live,
            available_at=NOW - timedelta(minutes=1),
        ),
    ]
    with job_database() as session:
        session.add_all(jobs)
        session.commit()
        job_ids = {job.task_id: job.id for job in jobs}

    with job_database() as session:
        recovered = recover_jobs(session, now=NOW)
        session.commit()
        assert set(recovered.requeued_job_ids) == {
            job_ids["recoverable"],
            job_ids["unleased-legacy"],
        }
        assert recovered.failed_job_ids == (job_ids["exhausted"],)
        assert recovered.cancelled_job_ids == (job_ids["cancelled-on-recovery"],)

    with job_database() as session:
        assert session.get(Job, job_ids["recoverable"]).status == "queued"
        assert session.get(Job, job_ids["unleased-legacy"]).status == "queued"
        assert session.get(Job, job_ids["exhausted"]).status == "failed"
        assert session.get(Job, job_ids["cancelled-on-recovery"]).status == "cancelled"
        assert session.get(Job, job_ids["live"]).status == "running"
        resumed = claim_job(
            session,
            worker_id="new-worker",
            job_id=job_ids["recoverable"],
            now=NOW,
        )
        assert resumed is not None and resumed.attempt == 2


def test_retryable_failure_honors_backoff_and_attempt_limit_then_manual_retry(
    job_database,
):
    with job_database() as session:
        job_id = enqueue(session, key="retry", max_attempts=2).job.id
        first = claim_job(session, worker_id="worker", job_id=job_id, now=NOW)
        assert first is not None and first.attempt == 1
        queued = fail_job(
            session,
            first,
            error="temporary provider failure",
            retryable=True,
            retry_delay_seconds=60,
            now=NOW + timedelta(seconds=1),
        )
        assert queued is not None and queued.status == "queued"
        session.commit()

    with job_database() as session:
        assert (
            claim_job(
                session,
                worker_id="worker",
                job_id=job_id,
                now=NOW + timedelta(seconds=59),
            )
            is None
        )
        second = claim_job(
            session,
            worker_id="worker",
            job_id=job_id,
            now=NOW + timedelta(seconds=61),
        )
        assert second is not None and second.attempt == 2
        terminal = fail_job(
            session,
            second,
            error="provider stayed unavailable",
            retryable=True,
            now=NOW + timedelta(seconds=62),
        )
        assert terminal is not None and terminal.status == "failed"
        session.commit()

    with job_database() as session:
        retried = retry_job(session, job_id, now=NOW + timedelta(seconds=63))
        repeated = retry_job(session, job_id, now=NOW + timedelta(seconds=63))
        assert retried.changed is True
        assert repeated.changed is False
        stored = session.get(Job, job_id)
        assert stored.status == "queued"
        assert stored.attempt == 0
        assert stored.error is None


def test_cancel_is_immediate_for_queued_and_cooperative_for_running_jobs(
    job_database,
):
    with job_database() as session:
        queued_id = enqueue(session, key="cancel-queued").job.id
        queued_cancel = cancel_job(session, queued_id, now=NOW)
        assert queued_cancel.status == "cancelled"
        assert queued_cancel.changed is True
        assert cancel_job(session, queued_id, now=NOW).changed is False
        assert (
            claim_job(
                session,
                worker_id="worker",
                job_id=queued_id,
                now=NOW,
            )
            is None
        )

        running_id = enqueue(session, key="cancel-running").job.id
        lease = claim_job(
            session,
            worker_id="worker",
            job_id=running_id,
            lease_seconds=60,
            now=NOW,
        )
        assert lease is not None
        request = cancel_job(session, running_id, now=NOW + timedelta(seconds=1))
        assert request.status == "running"
        assert request.changed is True
        heartbeat = heartbeat_job(
            session,
            lease,
            now=NOW + timedelta(seconds=2),
        )
        assert heartbeat.renewed is False
        assert heartbeat.cancel_requested is True
        assert (
            complete_job(
                session,
                lease,
                now=NOW + timedelta(seconds=2),
            )
            is None
        )
        acknowledged = cancel_job(
            session,
            running_id,
            lease=lease,
            now=NOW + timedelta(seconds=2),
        )
        assert acknowledged.status == "cancelled"
        assert acknowledged.changed is True


def test_content_0007_job_lease_migration_backfills_and_downgrades(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'content-job-leases.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_URL", database_url)
    config = Config(str(ROOT / "services" / "content_service" / "alembic.ini"))
    command.upgrade(config, "content_0006")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    id, task_id, owner_id, kind, status, progress, result_json,
                    error, created_at, updated_at
                ) VALUES (
                    'legacy-job', 'legacy-task', 'owner-a', 'video_composition',
                    'queued', 0, '{}', NULL, :created_at, :updated_at
                )
                """
            ),
            {"created_at": NOW, "updated_at": NOW},
        )

    command.upgrade(config, "content_0007")
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("jobs")}
    assert {
        "idempotency_key",
        "attempt",
        "max_attempts",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "heartbeat_at",
        "cancel_requested_at",
        "available_at",
        "completed_at",
    } <= columns
    with engine.connect() as connection:
        legacy = connection.execute(
            text("SELECT attempt, max_attempts, available_at FROM jobs WHERE id = 'legacy-job'")
        ).one()
    assert legacy.attempt == 0
    assert legacy.max_attempts == 3
    assert legacy.available_at is not None

    command.downgrade(config, "content_0006")
    downgraded_columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    assert "lease_token" not in downgraded_columns
    assert "attempt" not in downgraded_columns
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT status FROM jobs WHERE id = 'legacy-job'")).scalar_one()
            == "queued"
        )
    command.downgrade(config, "base")
    engine.dispose()
