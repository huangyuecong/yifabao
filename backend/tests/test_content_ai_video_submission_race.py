"""Two-connection submission/worker races; no network or application database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_content_video_project_generation import (
    MP4,
    FakeAiVideoProvider,
    FakeProviderMediaPublisher,
    context,
    create_video_reference_asset,
)
from test_content_video_project_generation import (
    generation_database as isolated_generation_database,
)

from content_service.job_leases import cancel_job, claim_job
from content_service.llm_adapters import ProviderCallError
from content_service.models import Artifact, Job, ProjectAsset
from content_service.providers import AiVideoTask
from content_service.services import dump, load
from content_service.video_project_generation import (
    VIDEO_PROJECT_AI_JOB_KIND,
    ProjectAiVideoGenerationSpec,
    enqueue_project_ai_video,
    restore_misclassified_project_ai_video_job,
)

generation_database = isolated_generation_database
OLD_ERROR = "AI 视频服务未能创建生成任务，请重新提交或更换参考素材"


class BlockingProvider(FakeAiVideoProvider):
    def __init__(self, *, fail=False, cancel_fails=False):
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.cancel_calls = []
        self.fail = fail
        self.cancel_fails = cancel_fails

    def submit(self, request):
        self.requests.append(request)
        self.entered.set()
        assert self.release.wait(10), "test did not release the simulated provider"
        if self.fail:
            raise ProviderCallError("模拟提交失败", retryable=False)
        return AiVideoTask(
            provider_task_id="already-submitted-provider-id",
            provider_id=self.status.provider_id,
            model=self.status.model,
            status="queued",
        )

    def cancel(self, provider_task_id):
        self.cancel_calls.append(provider_task_id)
        if self.cancel_fails:
            raise ProviderCallError("模拟取消服务暂不可用", retryable=True)
        return AiVideoTask(
            provider_task_id=provider_task_id,
            provider_id=self.status.provider_id,
            model=self.status.model,
            status="cancelled",
        )


def _submit(fixture, provider, spec, *, cleanup=False):
    from content_service import main

    factory, store, task_id, project_id = fixture
    with factory() as session:
        task, project = context(session, task_id, project_id)
        try:
            result = enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=spec,
            )
            session.commit()
            return result.job.id
        except Exception as exc:
            if cleanup:
                main.cleanup_failed_ai_video_submission(session, exc)
            raise


def _pending(session):
    return session.scalar(select(Job).where(Job.kind == VIDEO_PROJECT_AI_JOB_KIND))


@pytest.mark.parametrize("mode", ["full", "video"])
def test_blocked_submission_is_visible_but_unclaimable_in_second_sqlite_session(
    generation_database, monkeypatch, mode
):
    from content_service import main

    factory, store, task_id, project_id = generation_database
    with factory.begin() as session:
        task, _ = context(session, task_id, project_id)
        task.mode = mode
    monkeypatch.setattr(main, "SessionLocal", factory)
    provider = BlockingProvider()
    spec = ProjectAiVideoGenerationSpec(prompt="修改视频背景", idempotency_key="one-click")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_submit, generation_database, provider, spec)
        try:
            assert provider.entered.wait(5)
            with factory() as other:
                pending = _pending(other)
                assert pending is not None  # Real SQLite SAVEPOINT is externally visible.
                job_id = pending.id
                assert pending.status == "queued"
                assert pending.available_at > datetime.now(UTC) + timedelta(days=365)
                assert load(pending.result_json)["submission_state"] == "submitting"
                assert claim_job(other, worker_id="sweep", job_id=job_id) is None
                other.commit()

            assert main.recover_expired_video_project_jobs() == 0
            main.run_video_project_ai_generation_job(job_id, provider, store)
            with factory() as other:
                pending = other.get(Job, job_id)
                assert pending.attempt == 0
                assert pending.error is None
                assert pending.status == "queued"
                task, project = context(other, task_id, project_id)
                duplicate = enqueue_project_ai_video(
                    other,
                    project=project,
                    task=task,
                    created_by="owner-a",
                    provider=provider,
                    asset_store=store,
                    spec=spec,
                )
                assert duplicate.created is False
                assert duplicate.job.id == job_id
                assert len(provider.requests) == 1
        finally:
            provider.release.set()
        assert future.result(timeout=10) == job_id

    with factory() as other:
        completed_submission = other.get(Job, job_id)
        result = load(completed_submission.result_json)
        assert result["provider_task_id"] == "already-submitted-provider-id"
        assert result["submission_state"] == "submitted"
        assert completed_submission.available_at <= datetime.now(UTC)
        assert completed_submission.status == "queued"
        assert completed_submission.error is None
        assert completed_submission.attempt == 0
        lease = claim_job(other, worker_id="after-submit", job_id=job_id)
        assert lease is not None
        other.rollback()
        assert other.get(Job, job_id).status == "queued"


@pytest.mark.parametrize(
    "provider_fails,cancel_fails", [(False, False), (True, False), (False, True)]
)
def test_cancelling_while_provider_is_blocked_never_revives_cancelled_job(
    generation_database, provider_fails, cancel_fails
):
    factory, _, _, _ = generation_database
    provider = BlockingProvider(fail=provider_fails, cancel_fails=cancel_fails)
    spec = ProjectAiVideoGenerationSpec(prompt="修改视频", idempotency_key="cancel-in-flight")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_submit, generation_database, provider, spec, cleanup=True)
        try:
            assert provider.entered.wait(5)
            with factory() as other:
                job_id = _pending(other).id
                assert cancel_job(other, job_id).status == "cancelled"
                other.commit()
        finally:
            provider.release.set()
        if provider_fails:
            with pytest.raises(ProviderCallError):
                future.result(timeout=10)
        else:
            assert future.result(timeout=10) == job_id

    with factory() as other:
        job = other.get(Job, job_id)
        assert job is not None  # Cleanup must retain the user's cancellation tombstone.
        assert job.status == "cancelled"
        assert job.cancel_requested_at is not None
        assert job.completed_at is not None
        assert job.attempt == 0
        if not provider_fails:
            assert load(job.result_json)["provider_task_id"] == "already-submitted-provider-id"
            assert provider.cancel_calls == ["already-submitted-provider-id"]
        else:
            assert provider.cancel_calls == []
        assert claim_job(other, worker_id="must-not-revive", job_id=job_id) is None


def test_cancellation_during_reference_publication_prevents_billable_submit(generation_database):
    factory, store, task_id, project_id = generation_database
    with factory.begin() as session:
        task, project = context(session, task_id, project_id)
        reference = create_video_reference_asset(
            session, store, task, project, index=1, duration_seconds=5
        )
        reference_id = reference.id

    class CancellingPublisher(FakeProviderMediaPublisher):
        def publish(self, *args, **kwargs):
            with factory.begin() as other:
                assert cancel_job(other, _pending(other).id).status == "cancelled"
            return super().publish(*args, **kwargs)

    publisher = CancellingPublisher()
    provider = FakeAiVideoProvider()
    with factory() as session:
        task, project = context(session, task_id, project_id)
        result = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            provider_media_publisher=publisher,
            spec=ProjectAiVideoGenerationSpec(
                prompt="修改参考视频",
                reference_video_asset_ids=(reference_id,),
                idempotency_key="cancel-before-provider-submit",
            ),
        )
        session.commit()
        assert result.job.status == "cancelled"
        assert result.provider_task_id is None
    assert provider.requests == []
    assert len(publisher.published) == 1
    assert len(publisher.released) == 1


def test_failed_submit_cleans_its_visible_reservation_without_ghost_job(generation_database):
    factory, _, _, _ = generation_database
    provider = BlockingProvider(fail=True)
    spec = ProjectAiVideoGenerationSpec(prompt="修改视频", idempotency_key="failed-submit")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_submit, generation_database, provider, spec, cleanup=True)
        try:
            assert provider.entered.wait(5)
            with factory() as other:
                job_id = _pending(other).id
        finally:
            provider.release.set()
        with pytest.raises(ProviderCallError):
            future.result(timeout=10)
    with factory() as other:
        assert other.get(Job, job_id) is None


def test_worker_leaves_legacy_reservation_without_id_unclaimed(generation_database, monkeypatch):
    from content_service import main

    factory, store, task_id, project_id = generation_database
    with factory.begin() as session:
        job = Job(
            task_id=task_id,
            owner_id="owner-a",
            kind=VIDEO_PROJECT_AI_JOB_KIND,
            status="queued",
            result_json=dump({"project_id": project_id, "request": {"prompt": "修改视频"}}),
        )
        session.add(job)
        session.flush()
        job_id = job.id
    monkeypatch.setattr(main, "SessionLocal", factory)
    main.run_video_project_ai_generation_job(job_id, FakeAiVideoProvider(), store)
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.status == "queued" and job.attempt == 0
        assert job.error is None and job.completed_at is None


def _historical_job(fixture, **overrides):
    factory, _, task_id, project_id = fixture
    result = overrides.pop(
        "result",
        {
            "project_id": project_id,
            "provider_task_id": "existing-remote-job",
            "provider_status": "queued",
            "request": {"prompt": "不要字幕，背景亮一点", "duration_seconds": 5},
        },
    )
    fields = {
        "id": str(uuid4()),
        "task_id": task_id,
        "owner_id": "owner-a",
        "kind": VIDEO_PROJECT_AI_JOB_KIND,
        "status": "failed",
        "error": OLD_ERROR,
        "attempt": 1,
        "max_attempts": 3,
        "completed_at": datetime.now(UTC),
        "result_json": dump(result),
        **overrides,
    }
    with factory.begin() as session:
        session.add(Job(**fields))
        task, _ = context(session, task_id, project_id)
        task.audience = "成年人"
    return fields["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"status": "cancelled"},
        {"error": "模型内容审核拒绝"},
        {"cancel_requested_at": datetime.now(UTC)},
        {"lease_expires_at": datetime.now(UTC) + timedelta(minutes=5)},
        {"kind": "video_composition"},
        {"result": {"provider_status": "queued"}},
        {"result": {"provider_task_id": "other", "provider_status": "failed"}},
        {
            "result": {
                "provider_task_id": "other",
                "error_detail": {"code": "reference_image_real_person"},
            }
        },
    ],
)
def test_historical_recovery_never_overrides_other_failure_cancel_or_live_lease(
    generation_database, change
):
    factory, _, _, _ = generation_database
    job_id = _historical_job(generation_database, **change)
    with factory() as session:
        job = session.get(Job, job_id)
        before = (job.status, job.error, job.result_json, job.attempt, job.completed_at)
        assert restore_misclassified_project_ai_video_job(session, job_id) is False
        session.commit()
        assert (job.status, job.error, job.result_json, job.attempt, job.completed_at) == before


@pytest.mark.parametrize("mode", ["full", "video"])
def test_retry_recovers_same_provider_job_and_archives_without_another_submission(
    generation_database, monkeypatch, mode
):
    from content_service import main

    factory, store, task_id, project_id = generation_database
    job_id = _historical_job(generation_database)
    with factory.begin() as session:
        task, _ = context(session, task_id, project_id)
        task.mode = mode

    class ExistingProvider(FakeAiVideoProvider):
        def __init__(self):
            super().__init__()
            self.get_calls = []

        def submit(self, request):
            raise AssertionError("Recovering a known provider ID must never submit again")

        def get(self, provider_task_id):
            self.get_calls.append(provider_task_id)
            return AiVideoTask(
                provider_task_id=provider_task_id,
                provider_id=self.status.provider_id,
                model=self.status.model,
                status="succeeded",
                progress=100,
                video_url="https://test.invalid/existing-result.mp4",
            )

    provider = ExistingProvider()
    dispatched = []
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "dispatcher", SimpleNamespace(submit=dispatched.append))
    monkeypatch.setattr(main, "_require_external_provider_egress", lambda *_, **__: ())
    monkeypatch.setattr(main, "audit", lambda *_, **__: None)
    monkeypatch.setattr(
        main,
        "get_video_project_or_404",
        lambda session, _id, _identity: tuple(reversed(context(session, task_id, project_id))),
    )
    monkeypatch.setattr(
        main,
        "_transfer_project_ai_video",
        lambda _provider_task, *, asset_store, storage_key: asset_store.put_bytes(
            storage_key, MP4, "video/mp4"
        ),
    )
    with factory() as session:
        response = main.retry_job(
            job_id,
            session=session,
            identity=SimpleNamespace(user_id="owner-a", department="", roles=frozenset({"admin"})),
            ai_video_provider=provider,
            video_composer=None,
            asset_store=store,
            provider_media_publisher=SimpleNamespace(enabled=False),
            timeline_renderer=None,
        )
        assert response["id"] == job_id
        assert response["status"] == "queued"
        assert response["result"]["provider_task_id"] == "existing-remote-job"
        assert response["result"]["submission_recovery"]["previous_error"] == OLD_ERROR
        assert session.get(Job, job_id).attempt == 0
    assert len(dispatched) == 1
    dispatched[0]()
    main.run_video_project_ai_generation_job(job_id, provider, store)
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.status == "succeeded", job.error
        assert job.error is None
        assert provider.get_calls == ["existing-remote-job"]
        assert len(session.scalars(select(Job)).all()) == 1
        assert len(session.scalars(select(Artifact)).all()) == 1
        assert len(session.scalars(select(ProjectAsset)).all()) == 1
        assert context(session, task_id, project_id)[1].current_revision == 1
        assert restore_misclassified_project_ai_video_job(session, job_id) is False
