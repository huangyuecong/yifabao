from uuid import uuid4

from sqlalchemy import delete, func, select


def _create_task(client, headers):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": "copy",
            "name": f"永久删除-{uuid4().hex[:8]}",
            "topic": "删除测试任务",
            "product": "臻品雅酿",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_permanent_delete_removes_task_owned_rows(content_client, user_headers):
    from content_service.db import SessionLocal
    from content_service.models import (
        Artifact,
        ArtifactLink,
        EgressGrant,
        GenerationRun,
        Job,
    )

    task = _create_task(content_client, user_headers)
    task_id = task["id"]
    with SessionLocal.begin() as session:
        run = GenerationRun(
            task_id=task_id,
            stage_key="copy",
            status="succeeded",
            created_by="user-a",
        )
        session.add(run)
        session.flush()
        artifact = Artifact(
            task_id=task_id,
            kind="copy",
            stage_key="copy",
            generation_run_id=run.id,
            status="draft",
            content_json='{"body":"待删除文案"}',
            created_by="user-a",
        )
        session.add(artifact)
        session.flush()
        session.add(
            ArtifactLink(
                target_task_id=task_id,
                source_task_id=task_id,
                artifact_id=artifact.id,
                stage_key="copy",
                created_by="user-a",
            )
        )
        session.add(
            EgressGrant(
                task_id=task_id,
                provider="test-provider",
                approved_by="user-a",
            )
        )
        session.add(
            Job(
                task_id=task_id,
                generation_run_id=run.id,
                owner_id="user-a",
                kind="test-generation",
                status="succeeded",
            )
        )

    deleted = content_client.delete(
        f"/api/v1/tasks/{task_id}/permanent",
        headers=user_headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"id": task_id, "deleted": True}
    assert (
        content_client.get(
            f"/api/v1/tasks/{task_id}",
            headers=user_headers,
        ).status_code
        == 404
    )

    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ArtifactLink)
                .where(
                    ArtifactLink.target_task_id == task_id,
                )
            )
            == 0
        )
        for model in (Artifact, EgressGrant, Job, GenerationRun):
            assert (
                session.scalar(
                    select(func.count()).select_from(model).where(model.task_id == task_id)
                )
                == 0
            )


def test_permanent_delete_keeps_task_when_artifact_is_linked_elsewhere(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact, ArtifactLink

    source = _create_task(content_client, user_headers)
    target = _create_task(content_client, user_headers)
    source_id = source["id"]
    target_id = target["id"]
    with SessionLocal.begin() as session:
        artifact = Artifact(
            task_id=source_id,
            kind="copy",
            stage_key="copy",
            status="confirmed",
            content_json='{"body":"被引用文案"}',
            created_by="user-a",
        )
        session.add(artifact)
        session.flush()
        session.add(
            ArtifactLink(
                target_task_id=target_id,
                source_task_id=source_id,
                artifact_id=artifact.id,
                stage_key="copy",
                created_by="user-a",
            )
        )

    try:
        blocked = content_client.delete(
            f"/api/v1/tasks/{source_id}/permanent",
            headers=user_headers,
        )
        assert blocked.status_code == 409, blocked.text
        assert "其他任务引用" in blocked.json()["detail"]
        assert (
            content_client.get(
                f"/api/v1/tasks/{source_id}",
                headers=user_headers,
            ).status_code
            == 200
        )
    finally:
        with SessionLocal.begin() as session:
            session.execute(
                delete(ArtifactLink).where(
                    ArtifactLink.source_task_id == source_id,
                    ArtifactLink.target_task_id == target_id,
                )
            )
        for task_id in (source_id, target_id):
            response = content_client.delete(
                f"/api/v1/tasks/{task_id}/permanent",
                headers=user_headers,
            )
            assert response.status_code == 200, response.text


def test_permanent_delete_blocks_active_jobs(content_client, user_headers):
    from content_service.db import SessionLocal
    from content_service.models import Job

    task = _create_task(content_client, user_headers)
    task_id = task["id"]
    with SessionLocal.begin() as session:
        session.add(
            Job(
                task_id=task_id,
                owner_id="user-a",
                kind="copy-generation",
                status="running",
            )
        )

    blocked = content_client.delete(
        f"/api/v1/tasks/{task_id}/permanent",
        headers=user_headers,
    )
    assert blocked.status_code == 409, blocked.text
    assert "正在进行中" in blocked.json()["detail"]

    with SessionLocal.begin() as session:
        job = session.scalar(select(Job).where(Job.task_id == task_id))
        job.status = "succeeded"
    deleted = content_client.delete(
        f"/api/v1/tasks/{task_id}/permanent",
        headers=user_headers,
    )
    assert deleted.status_code == 200, deleted.text


def test_permanent_delete_removes_video_project_graph(content_client, user_headers):
    from content_service.db import SessionLocal
    from content_service.models import (
        AiEditProposal,
        Artifact,
        MediaDerivative,
        ProjectAsset,
        VideoProject,
        VideoProjectRevision,
        VideoUploadChunk,
        VideoUploadSession,
    )

    task = _create_task(content_client, user_headers)
    task_id = task["id"]
    with SessionLocal.begin() as session:
        artifact = Artifact(
            task_id=task_id,
            kind="copy",
            stage_key="copy",
            status="confirmed",
            content_json='{"body":"工程素材"}',
            created_by="user-a",
        )
        session.add(artifact)
        session.flush()
        project = VideoProject(
            task_id=task_id,
            current_revision_id="pending",
            created_by="user-a",
        )
        session.add(project)
        session.flush()
        parent = VideoProjectRevision(
            project_id=project.id,
            revision=1,
            source="initial",
            created_by="user-a",
        )
        session.add(parent)
        session.flush()
        child = VideoProjectRevision(
            project_id=project.id,
            revision=2,
            parent_revision_id=parent.id,
            source="human",
            created_by="user-a",
        )
        session.add(child)
        session.flush()
        project.current_revision_id = child.id
        asset = ProjectAsset(
            project_id=project.id,
            source_type="artifact",
            source_id=artifact.id,
            artifact_id=artifact.id,
            created_by="user-a",
        )
        session.add(asset)
        session.flush()
        session.add(
            MediaDerivative(
                project_asset_id=asset.id,
                kind="proxy",
                status="ready",
            )
        )
        upload = VideoUploadSession(
            project_id=project.id,
            owner_id="user-a",
            role="video",
            filename="clip.mp4",
            media_type="video/mp4",
            media_kind="video",
            total_size=1024,
            chunk_size=1024,
            total_chunks=1,
            checksum_sha256="a" * 64,
            project_asset_id=asset.id,
        )
        session.add(upload)
        session.flush()
        session.add(
            VideoUploadChunk(
                upload_session_id=upload.id,
                chunk_index=0,
                size_bytes=1024,
                checksum_sha256="b" * 64,
                storage_key=f"uploads/{upload.id}/0",
            )
        )
        session.add(
            AiEditProposal(
                project_id=project.id,
                base_revision_id=parent.id,
                candidate_revision_id=child.id,
                created_by="user-a",
            )
        )

    deleted = content_client.delete(
        f"/api/v1/tasks/{task_id}/permanent",
        headers=user_headers,
    )
    assert deleted.status_code == 200, deleted.text
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(VideoProject)
                .where(VideoProject.task_id == task_id)
            )
            == 0
        )
        for model in (
            VideoProjectRevision,
            ProjectAsset,
            MediaDerivative,
            VideoUploadSession,
            VideoUploadChunk,
            AiEditProposal,
            Artifact,
        ):
            if hasattr(model, "task_id"):
                remaining = session.scalar(
                    select(func.count()).select_from(model).where(model.task_id == task_id)
                )
            else:
                remaining = None
            if remaining is not None:
                assert remaining == 0


def test_permanent_delete_keeps_task_when_media_is_used_by_another_project(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact, ProjectAsset, VideoProject, VideoProjectRevision

    source = _create_task(content_client, user_headers)
    target = _create_task(content_client, user_headers)
    source_id = source["id"]
    target_id = target["id"]
    with SessionLocal.begin() as session:
        artifact = Artifact(
            task_id=source_id,
            kind="copy",
            stage_key="copy",
            status="confirmed",
            content_json='{"body":"跨工程引用"}',
            created_by="user-a",
        )
        session.add(artifact)
        session.flush()
        project = VideoProject(
            task_id=target_id,
            current_revision_id="pending",
            created_by="user-a",
        )
        session.add(project)
        session.flush()
        revision = VideoProjectRevision(
            project_id=project.id,
            revision=1,
            created_by="user-a",
        )
        session.add(revision)
        session.flush()
        project.current_revision_id = revision.id
        session.add(
            ProjectAsset(
                project_id=project.id,
                source_type="artifact",
                source_id=artifact.id,
                artifact_id=artifact.id,
                created_by="user-a",
            )
        )
        project_id = project.id

    try:
        blocked = content_client.delete(
            f"/api/v1/tasks/{source_id}/permanent",
            headers=user_headers,
        )
        assert blocked.status_code == 409, blocked.text
        assert "视频工程引用" in blocked.json()["detail"]
    finally:
        with SessionLocal.begin() as session:
            session.execute(delete(ProjectAsset).where(ProjectAsset.project_id == project_id))
            session.execute(
                delete(VideoProjectRevision).where(VideoProjectRevision.project_id == project_id)
            )
            session.execute(delete(VideoProject).where(VideoProject.id == project_id))
        for task_id in (source_id, target_id):
            response = content_client.delete(
                f"/api/v1/tasks/{task_id}/permanent",
                headers=user_headers,
            )
            assert response.status_code == 200, response.text
