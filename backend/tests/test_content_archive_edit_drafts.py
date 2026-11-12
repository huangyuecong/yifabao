import io
import json
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def archive_fixture(tmp_path, monkeypatch):
    from agent_core import LocalObjectStorage
    from content_service import main, services
    from content_service.db import Base, get_session
    from content_service.media_adapters import LocalAssetStore

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def session_dependency():
        with factory() as session:
            yield session

    store = LocalAssetStore(tmp_path / "objects")
    monkeypatch.setattr(main, "storage", LocalObjectStorage(tmp_path / "objects"))
    monkeypatch.setattr(services, "storage", LocalObjectStorage(tmp_path / "objects"))
    original = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_session] = session_dependency
    main.app.dependency_overrides[main.get_asset_store_port] = lambda: store
    try:
        yield TestClient(main.app), factory, store
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(original)
        engine.dispose()


def _create_archive(fixture, mode="image", *, legacy=False):
    from content_service.models import Artifact, ContentTask, TaskStage
    from content_service.workflow import ARTIFACT_STAGE, initialize_workflow

    _, factory, store = fixture
    stream = io.BytesIO()
    Image.new("RGB", (24, 32), "#1b756a").save(stream, "PNG")
    with factory.begin() as session:
        task = ContentTask(owner_id="user-a", name=f"归档隔离-{uuid4()}", mode=mode)
        session.add(task)
        session.flush()
        initialize_workflow(session, task, "user-a")
        kinds = {
            "image": ["image"],
            "copy": ["topic", "copy"],
            "full": ["topic", "copy", "image", "video"],
            "video": ["video"],
        }[mode]
        artifacts = []
        for kind in kinds:
            content = {
                "title": "原始标题",
                "text": "原始内容",
                "body": "原始文案",
                "media_type": "image/png",
            }
            if kind == "topic":
                content["candidates"] = ["原始标题"]
            storage_key = None
            if kind in {"image", "video"}:
                content["media_type"] = "image/png" if kind == "image" else "video/mp4"
                storage_key = f"tasks/{task.id}/{kind}.{'png' if kind == 'image' else 'mp4'}"
                store.put_bytes(
                    storage_key,
                    stream.getvalue() if kind == "image" else b"video-original",
                    content["media_type"],
                )
            artifact = Artifact(
                task_id=task.id,
                kind=kind,
                stage_key=ARTIFACT_STAGE[kind],
                created_by="user-a",
                status="confirmed",
                storage_key=storage_key,
                content_json=json.dumps(content),
            )
            session.add(artifact)
            artifacts.append(artifact)
        session.flush()
        for stage in session.scalars(select(TaskStage).where(TaskStage.task_id == task.id)):
            if stage.state == "skipped":
                continue
            mapping = {
                item.kind: [item.id] for item in artifacts if item.stage_key == stage.stage_key
            }
            stage.selected_artifact_ids_json = json.dumps(mapping)
            stage.confirmed_artifact_ids_json = json.dumps(mapping)
            stage.state = "confirmed"
            stage.confirmed_at = datetime.now(UTC)
        task.status = "confirmed"
        task.current_stage = "export"
        task.archived_at = None if legacy else datetime.now(UTC)
        task.archived_workflow_status = None if legacy else "confirmed"
        return task.id


def _snapshot(factory, task_id):
    from content_service.models import Artifact, ContentTask, TaskStage

    with factory() as session:
        task = session.get(ContentTask, task_id)
        rows = [
            task,
            *session.scalars(select(Artifact).where(Artifact.task_id == task_id)),
            *session.scalars(select(TaskStage).where(TaskStage.task_id == task_id)),
        ]
        return {
            row.id: {col.key: getattr(row, col.key) for col in row.__table__.columns}
            for row in rows
        }


@pytest.mark.parametrize("mode", ["copy", "image", "full", "video"])
def test_restore_is_hidden_separate_copy_and_only_final_confirmation_publishes(
    archive_fixture,
    user_headers,
    other_headers,
    mode,
):
    from content_service.archive_drafts import CREATED, PROMOTED
    from content_service.models import Artifact, AuditLog

    client, factory, _ = archive_fixture
    source_id = _create_archive(archive_fixture, mode)
    snapshot = _snapshot(factory, source_id)
    old_export = client.get(f"/api/v1/tasks/{source_id}/export", headers=user_headers)
    assert old_export.status_code == 200, old_export.text
    restored = client.post(f"/api/v1/tasks/{source_id}/restore", headers=user_headers)
    assert restored.status_code == 200, restored.text
    draft = restored.json()
    assert draft["id"] != source_id
    assert draft["is_edit_draft"] and draft["source_task_id"] == source_id
    assert not draft["archived"] and draft["status"] == "draft"
    assert (
        client.post(f"/api/v1/tasks/{source_id}/restore", headers=user_headers).json()["id"]
        == draft["id"]
    )
    assert client.get(f"/api/v1/tasks/{draft['id']}", headers=other_headers).status_code == 403
    for archived in (True, False):
        rows = client.get(
            "/api/v1/tasks",
            headers=user_headers,
            params={
                "include_archived": archived,
                "page_size": 1,
            },
        ).json()
        assert [item["id"] for item in rows] == ([source_id] if archived else [])
    assert _snapshot(factory, source_id) == snapshot
    assert (
        client.patch(
            f"/api/v1/tasks/{source_id}",
            headers=user_headers,
            json={
                "expected_version": draft["version"],
                "audience": "不允许修改归档",
            },
        ).status_code
        == 409
    )
    with factory() as session:
        originals = list(session.scalars(select(Artifact).where(Artifact.task_id == source_id)))
        copies = list(session.scalars(select(Artifact).where(Artifact.task_id == draft["id"])))
        assert {item.id for item in originals}.isdisjoint(item.id for item in copies)
        assert {(item.kind, item.storage_key) for item in originals} == {
            (item.kind, item.storage_key) for item in copies
        }
    workflow = client.get(f"/api/v1/tasks/{draft['id']}/workflow", headers=user_headers).json()
    export_stage = next(item for item in workflow["stages"] if item["key"] == "export")
    assert export_stage["state"] != "confirmed"
    assert set(workflow["export_artifact_ids"]) == {item.id for item in copies}
    payload = {"expected_version": export_stage["version"]}
    confirmed = client.post(
        f"/api/v1/tasks/{draft['id']}/stages/export/confirm", headers=user_headers, json=payload
    )
    assert confirmed.status_code == 200, confirmed.text
    again = client.post(
        f"/api/v1/tasks/{draft['id']}/stages/export/confirm", headers=user_headers, json=payload
    )
    assert again.status_code == 200 and again.json() == confirmed.json()
    published = client.get(f"/api/v1/tasks/{draft['id']}", headers=user_headers).json()
    assert published["archived"] and not published["is_edit_draft"]
    rows = client.get(
        "/api/v1/tasks", headers=user_headers, params={"include_archived": True}
    ).json()
    assert {item["id"] for item in rows} == {source_id, draft["id"]}
    with factory() as session:
        events = list(session.scalars(select(AuditLog).where(AuditLog.resource_id == draft["id"])))
        assert sum(event.action == CREATED for event in events) == 1
        assert sum(event.action == PROMOTED for event in events) == 1
    assert _snapshot(factory, source_id) == snapshot
    repeated_export = client.get(f"/api/v1/tasks/{source_id}/export", headers=user_headers)
    with (
        zipfile.ZipFile(io.BytesIO(old_export.content)) as old_zip,
        zipfile.ZipFile(io.BytesIO(repeated_export.content)) as new_zip,
    ):
        assert old_zip.namelist() == new_zip.namelist()
        for name in old_zip.namelist():
            before, after = old_zip.read(name), new_zip.read(name)
            if name.endswith(".docx"):
                # Word is freshly packaged on download; ZIP member timestamps
                # can change while its document content remains identical.
                with (
                    zipfile.ZipFile(io.BytesIO(before)) as old_doc,
                    zipfile.ZipFile(io.BytesIO(after)) as new_doc,
                ):
                    assert old_doc.read("word/document.xml") == new_doc.read("word/document.xml")
            else:
                assert before == after
    next_draft = client.post(f"/api/v1/tasks/{source_id}/restore", headers=user_headers).json()
    assert next_draft["id"] not in {source_id, draft["id"]}
    assert next_draft["is_edit_draft"]


def test_abandoned_edit_and_legacy_completed_archive_stay_isolated(archive_fixture, user_headers):
    from content_service.models import ContentTask

    client, factory, _ = archive_fixture
    source_id = _create_archive(archive_fixture, legacy=True)
    snapshot = _snapshot(factory, source_id)
    source = client.get(f"/api/v1/tasks/{source_id}", headers=user_headers).json()
    assert source["archived"] and source["archived_at"]
    assert source["workflow_status"] == "confirmed"
    draft = client.post(f"/api/v1/tasks/{source_id}/restore", headers=user_headers).json()
    updated = client.patch(
        f"/api/v1/tasks/{draft['id']}",
        headers=user_headers,
        json={
            "expected_version": draft["version"],
            "audience": "仅在草稿修改的新受众",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["is_edit_draft"]
    assert _snapshot(factory, source_id) == snapshot
    with factory() as session:
        assert session.get(ContentTask, source_id).archived_at is None
    assert client.get("/api/v1/tasks", headers=user_headers).json() == []
    history = client.get(
        "/api/v1/tasks", headers=user_headers, params={"include_archived": True}
    ).json()
    assert [item["id"] for item in history] == [source_id]


@pytest.mark.parametrize("state", ["draft", "active_job"])
def test_legacy_snapshot_detection_does_not_archive_unfinished_work(
    archive_fixture, user_headers, state
):
    from content_service.models import ContentTask, Job

    client, factory, _ = archive_fixture
    source_id = _create_archive(archive_fixture, legacy=True)
    with factory.begin() as session:
        if state == "draft":
            session.get(ContentTask, source_id).status = "draft"
        else:
            session.add(
                Job(
                    task_id=source_id,
                    owner_id="user-a",
                    kind="video_project_render",
                    status="running",
                )
            )
    source = client.get(f"/api/v1/tasks/{source_id}", headers=user_headers).json()
    assert source["archived"] is False
    assert (
        client.post(f"/api/v1/tasks/{source_id}/restore", headers=user_headers).status_code == 409
    )
    assert [row["id"] for row in client.get("/api/v1/tasks", headers=user_headers).json()] == [
        source_id
    ]
    if state == "active_job":
        for method, url, payload in [
            ("DELETE", f"/api/v1/tasks/{source_id}?expected_version={source['version']}", None),
            ("POST", f"/api/v1/tasks/{source_id}/stages/export/confirm", {"expected_version": 1}),
        ]:
            response = client.request(method, url, headers=user_headers, json=payload)
            assert response.status_code == 409
            assert "正在进行中" in response.json()["detail"]


@pytest.mark.parametrize("mode", ["full", "video"])
def test_video_revision_and_linked_artifact_media_have_independent_identities(
    archive_fixture,
    user_headers,
    mode,
    tmp_path,
):
    from content_service.archive_drafts import restore_archive_as_draft
    from content_service.models import (
        Artifact,
        ArtifactLink,
        ContentTask,
        GenerationRun,
        Job,
        MediaDerivative,
        ProjectAsset,
        VideoProject,
        VideoProjectRevision,
    )
    from content_service.timeline_media import FfmpegTimelineRenderer
    from content_service.video_project_rendering import prepare_video_project_render

    client, factory, store = archive_fixture
    image = io.BytesIO()
    Image.new("RGB", (96, 64), "#1b756a").save(image, "PNG")
    store.put_bytes("external/image.png", image.getvalue(), "image/png")
    source_id = _create_archive(archive_fixture, mode)
    with factory.begin() as session:
        source = session.get(ContentTask, source_id)
        other = ContentTask(owner_id="user-a", name="外部图片", mode="image")
        session.add(other)
        session.flush()
        linked = Artifact(
            task_id=other.id,
            kind="image",
            created_by="user-a",
            status="confirmed",
            storage_key="external/image.png",
            content_json='{"media_type":"image/png"}',
        )
        session.add(linked)
        session.flush()
        session.add(
            ArtifactLink(
                target_task_id=source_id,
                source_task_id=other.id,
                artifact_id=linked.id,
                stage_key="image",
                created_by="user-a",
            )
        )
        first_id = str(uuid4())
        project = VideoProject(task_id=source_id, current_revision_id=first_id, created_by="user-a")
        session.add(project)
        session.flush()
        asset = ProjectAsset(
            project_id=project.id,
            source_type="artifact",
            source_id=linked.id,
            artifact_id=linked.id,
            created_by="user-a",
        )
        session.add(asset)
        session.flush()
        timeline = {
            "canvas": {"width": 96, "height": 64, "fps": 25},
            "tracks": [
                {
                    "id": "main",
                    "kind": "video",
                    "items": [
                        {
                            "id": "clip",
                            "project_asset_id": asset.id,
                            "duration_seconds": 0.4,
                            "start_seconds": 0,
                            "metadata": {"source_artifact_id": linked.id},
                        }
                    ],
                }
            ],
        }
        first = VideoProjectRevision(
            id=first_id,
            project_id=project.id,
            revision=1,
            source="initial",
            timeline_json=json.dumps(timeline),
            created_by="user-a",
        )
        session.add(first)
        session.flush()
        second = VideoProjectRevision(
            project_id=project.id,
            revision=2,
            parent_revision_id=first.id,
            source="manual",
            timeline_json=json.dumps(timeline),
            operations_json=json.dumps([{"revision_id": first.id}]),
            created_by="user-a",
        )
        session.add(second)
        session.flush()
        project.current_revision = 2
        project.current_revision_id = second.id
        final = session.scalar(
            select(Artifact).where(Artifact.task_id == source_id, Artifact.kind == "video")
        )
        final.content_json = json.dumps(
            {
                "project_id": project.id,
                "revision_id": second.id,
                "purpose": "final_project_render",
                "timeline_composition_version": 2,
                "provider_task_id": source_id,
                "storage_key": f"tasks/{source_id}/video.mp4",
            }
        )
        session.add(
            GenerationRun(
                task_id=source_id, stage_key="image", created_by="user-a", status="running"
            )
        )
        session.add(
            Job(task_id=source_id, owner_id="user-a", kind="old-generation", status="running")
        )
        session.add(
            MediaDerivative(
                project_asset_id=asset.id,
                kind="thumbnail",
                status="ready",
                storage_key="external/thumbnail.png",
            )
        )
        session.flush()
        original_timeline = second.timeline_json
        draft = restore_archive_as_draft(session, source, "user-a")
        copied_project = session.scalar(
            select(VideoProject).where(VideoProject.task_id == draft.id)
        )
        assert copied_project.id != project.id
        copied_revision = session.get(VideoProjectRevision, copied_project.current_revision_id)
        assert copied_revision.id != second.id and copied_revision.parent_revision_id != first.id
        assert (
            session.get(VideoProjectRevision, copied_revision.parent_revision_id).project_id
            == copied_project.id
        )
        copied_asset = session.scalar(
            select(ProjectAsset).where(ProjectAsset.project_id == copied_project.id)
        )
        copied_artifact = session.get(Artifact, copied_asset.artifact_id)
        assert copied_asset.source_id == copied_artifact.id != linked.id
        assert copied_artifact.task_id == draft.id
        assert copied_artifact.storage_key == linked.storage_key
        copied_timeline = json.loads(copied_revision.timeline_json)
        assert copied_timeline["tracks"][0]["items"][0]["project_asset_id"] == copied_asset.id
        assert (
            copied_timeline["tracks"][0]["items"][0]["metadata"]["source_artifact_id"]
            == copied_artifact.id
        )
        copied_final = session.scalar(
            select(Artifact).where(Artifact.task_id == draft.id, Artifact.kind == "video")
        )
        content = json.loads(copied_final.content_json)
        assert (
            content["project_id"] == copied_project.id
            and content["revision_id"] == copied_revision.id
        )
        assert content["provider_task_id"] == source_id
        assert content["storage_key"] == f"tasks/{source_id}/video.mp4"
        assert (
            json.loads(copied_revision.operations_json)[0]["revision_id"]
            == copied_revision.parent_revision_id
        )
        assert (
            session.scalar(select(GenerationRun).where(GenerationRun.task_id == draft.id)).status
            == "cancelled"
        )
        assert not list(session.scalars(select(Job).where(Job.task_id == draft.id)))
        assert not list(
            session.scalars(select(ArtifactLink).where(ArtifactLink.target_task_id == draft.id))
        )
        assert (
            session.scalar(
                select(MediaDerivative).where(MediaDerivative.project_asset_id == copied_asset.id)
            ).storage_key
            == "external/thumbnail.png"
        )
        with prepare_video_project_render(
            session, copied_project, store, tmp_path / "render.mp4", work_root=tmp_path / "work"
        ) as prepared:
            result = FfmpegTimelineRenderer(allowed_input_roots=[tmp_path]).render(prepared.request)
            assert result.duration_seconds == pytest.approx(0.4, abs=0.05)
            assert result.output_path.is_file()
        copied_artifact.status = "discarded"
        assert linked.status == "confirmed" and second.timeline_json == original_timeline
    original_snapshot = _snapshot(factory, source_id)
    mutations = [
        ("PUT", f"/api/v1/tasks/{source_id}/access", {"user_ids": ["user-b"]}),
        (
            "POST",
            f"/api/v1/video-projects/{project.id}/edits",
            {
                "expected_revision": 2,
                "operations": [{"type": "update_canvas", "changes": {"fps": 30}}],
            },
        ),
        ("PATCH", f"/api/v1/video-projects/{project.id}/assets/{asset.id}", {"label": "不能改"}),
        ("DELETE", f"/api/v1/video-projects/{project.id}/assets/{asset.id}", None),
        ("POST", f"/api/v1/video-projects/{project.id}/assets/{asset.id}/confirm-brand", None),
        (
            "POST",
            f"/api/v1/video-projects/{project.id}/generate-music",
            {"expected_revision": 2, "prompt": "不应调用服务"},
        ),
        ("POST", f"/api/v1/video-projects/{project.id}/renders", {"expected_revision": 2}),
        ("POST", f"/api/v1/video-projects/{project.id}/exports", {"expected_revision": 2}),
        ("POST", f"/api/v1/artifacts/{final.id}/confirm", None),
        ("DELETE", f"/api/v1/artifacts/{final.id}", None),
        (
            "PATCH",
            f"/api/v1/artifacts/{final.id}",
            {"expected_version": final.version, "content": {}},
        ),
    ]
    for method, url, payload in mutations:
        response = client.request(method, url, headers=user_headers, json=payload)
        assert response.status_code == 409, (url, response.text)
        assert "归档" in response.json()["detail"]
    assert _snapshot(factory, source_id) == original_snapshot
    assert (
        client.get(
            f"/api/v1/video-projects/{project.id}/assets/{asset.id}/download",
            headers=user_headers,
        ).content
        == image.getvalue()
    )
    workflow = client.get(f"/api/v1/tasks/{draft.id}/workflow", headers=user_headers).json()
    confirmed = client.post(
        f"/api/v1/tasks/{draft.id}/stages/export/confirm",
        headers=user_headers,
        json={
            "expected_version": next(
                item for item in workflow["stages"] if item["key"] == "export"
            )["version"]
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    exported = client.get(f"/api/v1/tasks/{draft.id}/export", headers=user_headers)
    assert exported.status_code == 200, exported.text
