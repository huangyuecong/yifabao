from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, case, false, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Job

DEFAULT_LEASE_SECONDS = 120
MAX_LEASE_SECONDS = 24 * 60 * 60
MAX_RETRY_DELAY_SECONDS = 24 * 60 * 60
MAX_CLAIM_RETRIES = 5
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class JobLeaseError(RuntimeError):
    pass


class JobLeaseNotFoundError(JobLeaseError):
    pass


class JobLeaseConflictError(JobLeaseError):
    pass


class JobLeaseValidationError(JobLeaseError):
    pass


@dataclass(frozen=True, slots=True)
class JobEnqueueResult:
    job: Job
    created: bool


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    worker_id: str
    token: str
    attempt: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class JobHeartbeat:
    renewed: bool
    cancel_requested: bool
    status: str | None
    lease_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class JobTransition:
    job_id: str
    status: str
    changed: bool


@dataclass(frozen=True, slots=True)
class JobRecovery:
    requeued_job_ids: tuple[str, ...]
    failed_job_ids: tuple[str, ...]
    cancelled_job_ids: tuple[str, ...]


def _now(value: datetime | None) -> datetime:
    return value or datetime.now(UTC)


def _validate_lease_seconds(value: int) -> None:
    if value < 1 or value > MAX_LEASE_SECONDS:
        raise JobLeaseValidationError("租约时长必须在 1 秒到 24 小时之间")


def _validate_retry_delay(value: int) -> None:
    if value < 0 or value > MAX_RETRY_DELAY_SECONDS:
        raise JobLeaseValidationError("重试延迟必须在 0 秒到 24 小时之间")


def _json_payload(value: str | Mapping[str, object]) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise JobLeaseValidationError("任务结果必须可以序列化为 JSON") from exc


def _idempotent_job(
    session: Session,
    *,
    task_id: str,
    kind: str,
    idempotency_key: str,
) -> Job | None:
    return session.scalar(
        select(Job).where(
            Job.task_id == task_id,
            Job.kind == kind,
            Job.idempotency_key == idempotency_key,
        )
    )


def enqueue_job(
    session: Session,
    *,
    task_id: str,
    owner_id: str,
    kind: str,
    result_json: str | Mapping[str, object] = "{}",
    generation_run_id: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> JobEnqueueResult:
    """Create one durable job or return the prior row for the same request key."""

    if not task_id.strip() or not owner_id.strip() or not kind.strip():
        raise JobLeaseValidationError("task_id、owner_id 和 kind 不能为空")
    if max_attempts < 1 or max_attempts > 100:
        raise JobLeaseValidationError("max_attempts 必须在 1 到 100 之间")
    normalized_key = idempotency_key.strip() if idempotency_key is not None else None
    if normalized_key == "" or (normalized_key and len(normalized_key) > 128):
        raise JobLeaseValidationError("idempotency_key 长度必须在 1 到 128 之间")
    if normalized_key:
        existing = _idempotent_job(
            session,
            task_id=task_id,
            kind=kind,
            idempotency_key=normalized_key,
        )
        if existing is not None:
            return JobEnqueueResult(job=existing, created=False)

    job = Job(
        task_id=task_id,
        generation_run_id=generation_run_id,
        owner_id=owner_id,
        kind=kind,
        status="queued",
        progress=0,
        result_json=_json_payload(result_json),
        idempotency_key=normalized_key,
        attempt=0,
        max_attempts=max_attempts,
        available_at=available_at or _now(None),
    )
    if normalized_key is None:
        session.add(job)
        session.flush()
        return JobEnqueueResult(job=job, created=True)

    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = _idempotent_job(
            session,
            task_id=task_id,
            kind=kind,
            idempotency_key=normalized_key,
        )
        if existing is None:
            raise
        return JobEnqueueResult(job=existing, created=False)
    return JobEnqueueResult(job=job, created=True)


def _expired_or_unleased(now: datetime):
    return or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now)


def _claimable(now: datetime):
    return and_(
        Job.cancel_requested_at.is_(None),
        Job.attempt < Job.max_attempts,
        Job.available_at <= now,
        or_(
            Job.status == "queued",
            and_(Job.status == "running", _expired_or_unleased(now)),
        ),
    )


def claim_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    job_id: str | None = None,
    kinds: Sequence[str] | None = None,
    now: datetime | None = None,
) -> JobLease | None:
    """Atomically claim one eligible job using an UPDATE compare-and-set fence."""

    if not worker_id.strip() or len(worker_id) > 128:
        raise JobLeaseValidationError("worker_id 长度必须在 1 到 128 之间")
    _validate_lease_seconds(lease_seconds)
    current_time = _now(now)
    deadline = current_time + timedelta(seconds=lease_seconds)
    candidate_query = select(Job.id).where(_claimable(current_time))
    if job_id is not None:
        candidate_query = candidate_query.where(Job.id == job_id)
    if kinds is not None:
        normalized_kinds = tuple(dict.fromkeys(item for item in kinds if item))
        if not normalized_kinds:
            return None
        candidate_query = candidate_query.where(Job.kind.in_(normalized_kinds))
    candidate_id = (
        candidate_query.order_by(Job.available_at, Job.created_at, Job.id)
        .limit(1)
        .scalar_subquery()
    )
    for _ in range(1 if job_id is not None else MAX_CLAIM_RETRIES):
        token = uuid4().hex
        claimed = session.execute(
            update(Job)
            .where(Job.id == candidate_id, _claimable(current_time))
            .values(
                status="running",
                attempt=Job.attempt + 1,
                lease_owner=worker_id,
                lease_token=token,
                lease_expires_at=deadline,
                heartbeat_at=current_time,
                error=None,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            continue
        claimed_row = session.execute(
            select(Job.id, Job.attempt).where(
                Job.lease_owner == worker_id,
                Job.lease_token == token,
            )
        ).one_or_none()
        if claimed_row is None:
            return None
        return JobLease(
            job_id=claimed_row.id,
            worker_id=worker_id,
            token=token,
            attempt=claimed_row.attempt,
            expires_at=deadline,
        )
    return None


def _active_lease(lease: JobLease, now: datetime):
    return and_(
        Job.id == lease.job_id,
        Job.status == "running",
        Job.lease_owner == lease.worker_id,
        Job.lease_token == lease.token,
        Job.attempt == lease.attempt,
        Job.lease_expires_at.is_not(None),
        Job.lease_expires_at > now,
    )


def heartbeat_job(
    session: Session,
    lease: JobLease,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    progress: int | None = None,
    now: datetime | None = None,
) -> JobHeartbeat:
    _validate_lease_seconds(lease_seconds)
    if progress is not None and not 0 <= progress <= 99:
        raise JobLeaseValidationError("运行中进度必须在 0 到 99 之间")
    current_time = _now(now)
    deadline = current_time + timedelta(seconds=lease_seconds)
    values: dict[str, object] = {
        "heartbeat_at": current_time,
        "lease_expires_at": deadline,
        "updated_at": current_time,
    }
    if progress is not None:
        values["progress"] = case(
            (Job.progress < progress, progress),
            else_=Job.progress,
        )
    renewed = session.execute(
        update(Job)
        .where(
            _active_lease(lease, current_time),
            Job.cancel_requested_at.is_(None),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if renewed.rowcount == 1:
        return JobHeartbeat(
            renewed=True,
            cancel_requested=False,
            status="running",
            lease_expires_at=deadline,
        )
    snapshot = session.execute(
        select(Job.status, Job.cancel_requested_at, Job.lease_expires_at).where(
            Job.id == lease.job_id
        )
    ).one_or_none()
    return JobHeartbeat(
        renewed=False,
        cancel_requested=bool(snapshot and snapshot.cancel_requested_at is not None),
        status=snapshot.status if snapshot else None,
        lease_expires_at=snapshot.lease_expires_at if snapshot else None,
    )


def complete_job(
    session: Session,
    lease: JobLease,
    *,
    result_json: str | Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> JobTransition | None:
    current_time = _now(now)
    values: dict[str, object] = {
        "status": "succeeded",
        "progress": 100,
        "error": None,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": current_time,
        "completed_at": current_time,
        "updated_at": current_time,
    }
    if result_json is not None:
        values["result_json"] = _json_payload(result_json)
    completed = session.execute(
        update(Job)
        .where(
            _active_lease(lease, current_time),
            Job.cancel_requested_at.is_(None),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if completed.rowcount != 1:
        return None
    return JobTransition(job_id=lease.job_id, status="succeeded", changed=True)


def fail_job(
    session: Session,
    lease: JobLease,
    *,
    error: str,
    retryable: bool,
    retry_delay_seconds: int = 0,
    now: datetime | None = None,
) -> JobTransition | None:
    _validate_retry_delay(retry_delay_seconds)
    current_time = _now(now)
    retry_at = current_time + timedelta(seconds=retry_delay_seconds)
    retry_condition = (
        and_(Job.cancel_requested_at.is_(None), Job.attempt < Job.max_attempts)
        if retryable
        else false()
    )
    cancel_condition = Job.cancel_requested_at.is_not(None)
    failed = session.execute(
        update(Job)
        .where(_active_lease(lease, current_time))
        .values(
            status=case(
                (cancel_condition, "cancelled"),
                (retry_condition, "queued"),
                else_="failed",
            ),
            progress=case((retry_condition, 0), else_=Job.progress),
            error=case(
                (cancel_condition, Job.error),
                else_=str(error)[:2000],
            ),
            available_at=case(
                (retry_condition, retry_at),
                else_=Job.available_at,
            ),
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            completed_at=case((retry_condition, None), else_=current_time),
            updated_at=current_time,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount != 1:
        return None
    status = session.scalar(select(Job.status).where(Job.id == lease.job_id))
    return JobTransition(
        job_id=lease.job_id,
        status=str(status),
        changed=True,
    )


def _job_transition(session: Session, job_id: str, *, changed: bool) -> JobTransition:
    status = session.scalar(select(Job.status).where(Job.id == job_id))
    if status is None:
        raise JobLeaseNotFoundError("媒体任务不存在")
    return JobTransition(job_id=job_id, status=status, changed=changed)


def cancel_job(
    session: Session,
    job_id: str,
    *,
    lease: JobLease | None = None,
    now: datetime | None = None,
) -> JobTransition:
    """Request cancellation, or acknowledge it immediately from the active worker."""

    current_time = _now(now)
    if lease is not None:
        if lease.job_id != job_id:
            raise JobLeaseValidationError("租约与待取消任务不匹配")
        acknowledged = session.execute(
            update(Job)
            .where(_active_lease(lease, current_time))
            .values(
                status="cancelled",
                cancel_requested_at=case(
                    (Job.cancel_requested_at.is_(None), current_time),
                    else_=Job.cancel_requested_at,
                ),
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                completed_at=current_time,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        return _job_transition(
            session,
            job_id,
            changed=acknowledged.rowcount == 1,
        )

    queued = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(
            status="cancelled",
            cancel_requested_at=current_time,
            completed_at=current_time,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            updated_at=current_time,
        )
        .execution_options(synchronize_session=False)
    )
    if queued.rowcount == 1:
        return JobTransition(job_id=job_id, status="cancelled", changed=True)
    running = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == "running",
            Job.cancel_requested_at.is_(None),
        )
        .values(cancel_requested_at=current_time, updated_at=current_time)
        .execution_options(synchronize_session=False)
    )
    return _job_transition(session, job_id, changed=running.rowcount == 1)


def recover_jobs(
    session: Session,
    *,
    kinds: Sequence[str] | None = None,
    now: datetime | None = None,
    limit: int = 1000,
) -> JobRecovery:
    """Recover abandoned jobs after restart without stealing a live lease."""

    if limit < 1 or limit > 10_000:
        raise JobLeaseValidationError("恢复批次大小必须在 1 到 10000 之间")
    current_time = _now(now)
    candidate = select(
        Job.id,
        Job.status,
        Job.attempt,
        Job.max_attempts,
        Job.cancel_requested_at,
    ).where(
        or_(
            and_(Job.status == "running", _expired_or_unleased(current_time)),
            and_(
                Job.status == "queued",
                or_(
                    Job.cancel_requested_at.is_not(None),
                    Job.attempt >= Job.max_attempts,
                ),
            ),
        )
    )
    if kinds is not None:
        normalized_kinds = tuple(dict.fromkeys(item for item in kinds if item))
        if not normalized_kinds:
            return JobRecovery((), (), ())
        candidate = candidate.where(Job.kind.in_(normalized_kinds))
    rows = session.execute(candidate.order_by(Job.created_at).limit(limit)).all()
    requeued: list[str] = []
    failed: list[str] = []
    cancelled: list[str] = []
    for row in rows:
        base_predicate = [Job.id == row.id, Job.status == row.status]
        if row.status == "running":
            base_predicate.append(_expired_or_unleased(current_time))
        if row.cancel_requested_at is not None:
            transition = session.execute(
                update(Job)
                .where(*base_predicate, Job.cancel_requested_at.is_not(None))
                .values(
                    status="cancelled",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    completed_at=current_time,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if transition.rowcount == 1:
                cancelled.append(row.id)
            continue
        if row.attempt >= row.max_attempts:
            transition = session.execute(
                update(Job)
                .where(
                    *base_predicate,
                    Job.cancel_requested_at.is_(None),
                    Job.attempt >= Job.max_attempts,
                )
                .values(
                    status="failed",
                    error=case(
                        (Job.error.is_(None), "任务租约过期且已达到最大尝试次数"),
                        else_=Job.error,
                    ),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    completed_at=current_time,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if transition.rowcount == 1:
                failed.append(row.id)
            continue
        transition = session.execute(
            update(Job)
            .where(
                *base_predicate,
                Job.cancel_requested_at.is_(None),
                Job.attempt < Job.max_attempts,
            )
            .values(
                status="queued",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                available_at=current_time,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if transition.rowcount == 1:
            requeued.append(row.id)
    return JobRecovery(tuple(requeued), tuple(failed), tuple(cancelled))


def retry_job(
    session: Session,
    job_id: str,
    *,
    max_attempts: int | None = None,
    now: datetime | None = None,
) -> JobTransition:
    if max_attempts is not None and not 1 <= max_attempts <= 100:
        raise JobLeaseValidationError("max_attempts 必须在 1 到 100 之间")
    current_time = _now(now)
    values: dict[str, object] = {
        "status": "queued",
        "progress": 0,
        "error": None,
        "attempt": 0,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "cancel_requested_at": None,
        "available_at": current_time,
        "completed_at": None,
        "updated_at": current_time,
    }
    if max_attempts is not None:
        values["max_attempts"] = max_attempts
    retried = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status.in_(("failed", "cancelled")))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if retried.rowcount == 1:
        return JobTransition(job_id=job_id, status="queued", changed=True)
    transition = _job_transition(session, job_id, changed=False)
    if transition.status == "queued":
        return transition
    raise JobLeaseConflictError("只有失败或已取消的任务可以重试")
