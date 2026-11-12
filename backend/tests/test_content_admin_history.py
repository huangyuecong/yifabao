"""Project administrators can inspect history without acquiring editing rights."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from agent_core.identity import get_identity as get_entry_identity
from agent_core.storage import LocalObjectStorage
from content_service import db, main, services
from content_service.access_control import ContentIdentity
from content_service.media_adapters import LocalAssetStore
from content_service.models import (
    Artifact,
    ArtifactLink,
    ContentTask,
    Job,
    LocalAccessLock,
    LocalUser,
    ProjectAsset,
)


def account(subject="project-admin", *, admin=False, verified=True, **claims):
    return ContentIdentity(
        user_id=subject,
        name=subject,
        department="review-department",
        roles=frozenset({"user"}),
        entry_verified=verified,
        is_project_admin=admin,
        **claims,
    )


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "session")
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'history.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr(main, "SessionLocal", factory)
    storage = LocalObjectStorage(tmp_path / "objects")
    monkeypatch.setattr(main, "storage", storage)
    monkeypatch.setattr(services, "storage", storage)
    asset_store = LocalAssetStore(tmp_path / "objects")
    state = SimpleNamespace(identity=account(admin=True))
    provider = Mock()
    old_overrides = main.app.dependency_overrides.copy()
    main.app.dependency_overrides.update({
        main.get_identity: lambda: state.identity,
        main.get_asset_store_port: lambda: asset_store,
        main.get_text_provider: lambda: provider,
        main.get_ai_video_generation_port: lambda: provider,
        main.get_provider_media_publisher: lambda: provider,
    })
    with factory.begin() as session:
        own = ContentTask(owner_id="project-admin", name="管理员自己的任务", mode="copy")
        private = ContentTask(owner_id="author", name="他人视频创作", mode="video")
        legacy = ContentTask(
            owner_id="dev-user", name="旧版归档文案", mode="copy", status="archived"
        )
        shared_user = ContentTask(
            owner_id="author", name="按人员共享", mode="copy", allowed_users_json='["reader"]'
        )
        shared_department = ContentTask(
            owner_id="author", name="按部门共享", mode="copy",
            allowed_departments_json='["review-department"]',
        )
        session.add_all([own, private, legacy, shared_user, shared_department])
        session.flush()
        copy_v1 = Artifact(
            task_id=legacy.id, kind="copy", version=1, created_by="dev-user",
            content_json=json.dumps({"title": "旧标题", "body": "旧版正文"}),
        )
        copy_v2 = Artifact(
            task_id=legacy.id, kind="copy", version=2, created_by="dev-user",
            content_json=json.dumps({"title": "新标题", "body": "新版本正文"}),
        )
        video = Artifact(
            task_id=private.id, kind="video", created_by="author",
            content_json='{"media_type":"video/mp4"}', storage_key="history/video.mp4",
        )
        session.add_all([copy_v1, copy_v2, video])
        session.flush()
        project, _ = services.create_or_get_video_project(session, private, "author")
        asset = ProjectAsset(
            project_id=project.id, source_type="artifact", source_id=video.id,
            artifact_id=video.id, created_by="author", label="历史视频.mp4",
        )
        done = Job(
            task_id=private.id, owner_id="author", kind="video_composition", status="succeeded",
            result_json='{"storage_key":"history/video.mp4"}',
        )
        running = Job(
            task_id=private.id, owner_id="author", kind="ai_video_generation", status="queued"
        )
        link = ArtifactLink(
            target_task_id=own.id, source_task_id=legacy.id, artifact_id=copy_v2.id,
            stage_key="copy", created_by="project-admin",
        )
        session.add_all([asset, done, running, link])
        session.flush()
        state.tasks = SimpleNamespace(
            own=own.id, private=private.id, legacy=legacy.id,
            shared_user=shared_user.id, shared_department=shared_department.id,
        )
        state.copy_ids = [copy_v1.id, copy_v2.id]
        state.video_id = video.id
        state.project_id = project.id
        state.asset_id = asset.id
        state.done_id = done.id
        state.running_id = running.id
    storage.path_for("history/video.mp4").write_bytes(b"0123456789")
    state.factory = factory
    state.provider = provider
    client = TestClient(main.app)
    state.client = client
    try:
        yield state
    finally:
        client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(old_overrides)
        engine.dispose()


def test_admin_history_includes_other_authors_and_legacy_archive_without_reassigning(history):
    client = history.client
    rows = client.get("/api/v1/tasks?include_archived=true").json()
    assert {row["id"] for row in rows} == set(vars(history.tasks).values())
    legacy = next(row for row in rows if row["id"] == history.tasks.legacy)
    assert legacy["owner_id"] == "dev-user" and legacy["can_edit"] is False
    own = next(row for row in rows if row["id"] == history.tasks.own)
    assert own["can_edit"] is True
    detail = client.get(f"/api/v1/tasks/{history.tasks.private}")
    assert detail.status_code == 200 and detail.json()["can_edit"] is False
    page_ids = set()
    for page in range(1, 4):
        response = client.get(f"/api/v1/tasks?include_archived=true&page_size=2&page={page}")
        assert response.status_code == 200
        page_ids.update(row["id"] for row in response.json())
    assert page_ids == set(vars(history.tasks).values())
    filtered = client.get("/api/v1/tasks", params={"status": "archived", "include_archived": True})
    assert [row["id"] for row in filtered.json()] == [history.tasks.legacy]
    with history.factory() as session:
        assert session.get(ContentTask, history.tasks.legacy).owner_id == "dev-user"
        assert session.get(ContentTask, history.tasks.private).allowed_users_json == "[]"


def test_admin_reads_all_versions_linked_content_and_downloads(history):
    client = history.client
    response = client.get(f"/api/v1/tasks/{history.tasks.legacy}/artifacts")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == set(history.copy_ids)
    linked = client.get(f"/api/v1/tasks/{history.tasks.own}/artifacts")
    assert linked.status_code == 200
    assert any(row["id"] == history.copy_ids[-1] and row["linked"] for row in linked.json())
    download = client.get(f"/api/v1/artifacts/{history.copy_ids[-1]}/download")
    assert download.status_code == 200 and "新版本正文" in download.content.decode("utf-8-sig")
    download = client.get(f"/api/v1/artifacts/{history.video_id}/download")
    assert download.status_code == 200 and download.content == b"0123456789"


def test_admin_reads_video_project_assets_range_playback_and_job_result(history):
    client = history.client
    base = f"/api/v1/video-projects/{history.project_id}"
    for path in (base, base + "/revisions", base + "/assets", base + "/jobs"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    asset_path = base + f"/assets/{history.asset_id}"
    assert client.get(asset_path).status_code == 200
    download = client.get(asset_path + "/download")
    assert download.status_code == 200 and download.content == b"0123456789"
    stream = client.get(asset_path + "/stream", headers={"Range": "bytes=2-5"})
    assert stream.status_code == 206 and stream.content == b"2345"
    assert stream.headers["content-range"] == "bytes 2-5/10"
    assert client.get(f"/api/v1/jobs/{history.done_id}").status_code == 200
    job_download = client.get(f"/api/v1/jobs/{history.done_id}/download")
    assert job_download.status_code == 200 and job_download.content == b"0123456789"


@pytest.mark.parametrize("identity", [
    account("reader"),
    account("reader", permissions=frozenset({"models.manage"})),
    account("reader", permissions=frozenset({"audit.read"})),
    account("reader", entry_is_admin=True, manageable_agent_keys=frozenset({"B8"})),
    replace(account("reader"), roles=frozenset({"admin", "reviewer", "user"})),
    account("reader", admin=True, verified=False),
])
def test_non_project_grants_and_unverified_admin_cannot_read_all_history(history, identity):
    history.identity = identity
    client = history.client
    response = client.get("/api/v1/tasks?include_archived=true")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {
        history.tasks.shared_user, history.tasks.shared_department,
    }
    for path in (
        f"/api/v1/tasks/{history.tasks.private}",
        f"/api/v1/tasks/{history.tasks.legacy}/artifacts",
        f"/api/v1/artifacts/{history.video_id}/download",
        f"/api/v1/video-projects/{history.project_id}",
        f"/api/v1/video-projects/{history.project_id}/assets/{history.asset_id}/stream",
        f"/api/v1/jobs/{history.done_id}",
        f"/api/v1/jobs/{history.done_id}/download",
    ):
        result = client.get(path)
        assert result.status_code == 403, (path, result.text)


def test_owner_and_explicit_collaborator_keep_existing_access(history):
    history.identity = account("author")
    own = history.client.get(f"/api/v1/tasks/{history.tasks.private}")
    assert own.status_code == 200 and own.json()["can_edit"] is True
    updated = history.client.patch(
        f"/api/v1/tasks/{history.tasks.private}",
        json={"expected_version": 1, "name": "作者修改"},
    )
    assert updated.status_code == 200, updated.text
    history.identity = account("reader")
    for task_id in (history.tasks.shared_user, history.tasks.shared_department):
        response = history.client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200 and response.json()["can_edit"] is True
    response = history.client.patch(
        f"/api/v1/tasks/{history.tasks.shared_user}",
        json={"expected_version": 1, "name": "协作者修改"},
    )
    assert response.status_code == 200, response.text


def test_admin_read_permission_does_not_allow_mutating_others_content(history):
    client = history.client
    task = f"/api/v1/tasks/{history.tasks.private}"
    artifact = f"/api/v1/artifacts/{history.video_id}"
    project = f"/api/v1/video-projects/{history.project_id}"
    writes = [
        ("PATCH", task, {"expected_version": 1, "name": "越权修改"}),
        ("DELETE", task + "?expected_version=1", None),
        ("DELETE", task + "/permanent", None),
        ("POST", task + "/restore", None),
        ("PUT", task + "/access", {"user_ids": ["project-admin"]}),
        ("POST", task + "/generate-copy", {}),
        ("PATCH", artifact, {"expected_version": 1, "content": {"body": "越权修改"}}),
        ("DELETE", artifact, None),
        ("POST", artifact + "/confirm", {}),
        ("POST", project + "/edits", {
            "expected_revision": 1,
            "operations": [{"type": "update_canvas", "changes": {"width": 1280}}],
        }),
        ("PATCH", project + f"/assets/{history.asset_id}", {"label": "越权修改"}),
        ("DELETE", project + f"/assets/{history.asset_id}", None),
        ("POST", f"/api/v1/jobs/{history.running_id}/cancel", None),
    ]
    for method, path, payload in writes:
        response = client.request(method, path, json=payload)
        assert response.status_code == 403, (method, path, response.status_code, response.text)
    history.provider.assert_not_called()
    assert history.provider.mock_calls == []
    with history.factory() as session:
        assert session.get(ContentTask, history.tasks.private).name == "他人视频创作"
        assert session.get(ContentTask, history.tasks.private).owner_id == "author"
        assert session.get(ProjectAsset, history.asset_id).active
        assert session.get(Job, history.running_id).status == "queued"
        assert session.scalar(select(Artifact).where(Artifact.id == history.video_id)).status == "draft"


def test_revoking_project_admin_immediately_removes_global_history_read(history):
    path = f"/api/v1/tasks/{history.tasks.private}"
    assert history.client.get(path).status_code == 200
    history.identity = replace(history.identity, is_project_admin=False)
    assert history.client.get(path).status_code == 403
    rows = history.client.get("/api/v1/tasks?include_archived=true").json()
    assert history.tasks.private not in {row["id"] for row in rows}


def test_development_mode_cannot_turn_project_admin_flag_into_global_read(history, monkeypatch):
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    response = history.client.get(f"/api/v1/tasks/{history.tasks.private}")
    assert response.status_code == 403


def test_verified_session_reads_current_local_admin_grant_on_every_request(history):
    # Only the already-verified upstream identity is supplied here. The real
    # business identity dependency must resolve and refresh its database grant.
    main.app.dependency_overrides.pop(main.get_identity)
    raw = replace(
        account(), roles=frozenset({"admin", "user"}), entry_is_admin=True,
        manageable_agent_keys=frozenset({"B8"}),
    )
    main.app.dependency_overrides[get_entry_identity] = lambda: raw
    with history.factory.begin() as session:
        session.add(LocalAccessLock(id=1))
        user = LocalUser(
            subject=raw.user_id, display_name=raw.name, department=raw.department,
            is_project_admin=True,
        )
        session.add(user)
        session.flush()
        local_id = user.id
    path = f"/api/v1/tasks/{history.tasks.private}"
    assert history.client.get(path).status_code == 200
    with history.factory.begin() as session:
        session.get(LocalUser, local_id).is_project_admin = False
    # Upstream roles and entry administrator claims cannot retain global read.
    assert history.client.get(path).status_code == 403
    rows = history.client.get("/api/v1/tasks?include_archived=true").json()
    assert history.tasks.private not in {row["id"] for row in rows}
