"""Permanent deletion of one content task and its task-owned records.

The content service intentionally keeps the public DELETE task endpoint as the
legacy archive operation. History uses this isolated operation when the user
explicitly chooses permanent deletion.
"""

from __future__ import annotations

import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    AiEditProposal,
    Artifact,
    ArtifactLink,
    AuditLog,
    ContentTask,
    EgressGrant,
    GenerationRun,
    Job,
    MediaDerivative,
    ProjectAsset,
    TaskStage,
    VideoProject,
    VideoProjectRevision,
    VideoUploadChunk,
    VideoUploadSession,
)


class TaskDeletionConflictError(RuntimeError):
    """Raised when deleting a task would damage content owned by another task."""


ACTIVE_JOB_STATUSES = ("queued", "running", "retrying")


def _ensure_deletion_isolated(
    session: Session,
    task: ContentTask,
    artifact_ids: list[str],
    project: VideoProject | None,
) -> None:
    active_job = session.scalar(
        select(Job.id).where(Job.task_id == task.id, Job.status.in_(ACTIVE_JOB_STATUSES)).limit(1)
    )
    if active_job is not None:
        raise TaskDeletionConflictError("仍有生成或导出正在进行中，请完成后再删除")

    if artifact_ids:
        external_link = session.scalar(
            select(ArtifactLink.id)
            .where(
                ArtifactLink.artifact_id.in_(artifact_ids),
                ArtifactLink.target_task_id != task.id,
            )
            .limit(1)
        )
        if external_link is not None:
            raise TaskDeletionConflictError(
                "该任务的内容版本已被其他任务引用，请先解除引用后再删除"
            )

        external_project_asset = select(ProjectAsset.id).where(
            ProjectAsset.artifact_id.in_(artifact_ids),
        )
        if project is not None:
            external_project_asset = external_project_asset.where(
                ProjectAsset.project_id != project.id,
            )
        if session.scalar(external_project_asset.limit(1)) is not None:
            raise TaskDeletionConflictError(
                "该任务的媒体版本已被其他视频工程引用，请先解除引用后再删除"
            )


def delete_content_task_records(
    session: Session,
    task: ContentTask,
    *,
    deleted_by: str,
) -> None:
    """Delete task-owned rows while preserving shared enterprise references."""

    artifacts = list(session.scalars(select(Artifact).where(Artifact.task_id == task.id)))
    artifact_ids = [item.id for item in artifacts]
    project = session.scalar(select(VideoProject).where(VideoProject.task_id == task.id))
    _ensure_deletion_isolated(session, task, artifact_ids, project)

    project_asset_ids: list[str] = []
    project_ids: list[str] = []
    upload_session_ids: list[str] = []
    revision_rows: list[VideoProjectRevision] = []
    if project is not None:
        project_ids.append(project.id)
        project_assets = list(
            session.scalars(select(ProjectAsset).where(ProjectAsset.project_id == project.id))
        )
        project_asset_ids = [item.id for item in project_assets]
        upload_session_ids = [
            item.id
            for item in session.scalars(
                select(VideoUploadSession).where(VideoUploadSession.project_id == project.id)
            )
        ]
        revision_rows = list(
            session.scalars(
                select(VideoProjectRevision).where(VideoProjectRevision.project_id == project.id)
            )
        )

    generation_run_ids = [
        item.id
        for item in session.scalars(select(GenerationRun).where(GenerationRun.task_id == task.id))
    ]

    session.add(
        AuditLog(
            owner_id=deleted_by,
            action="task.deleted",
            resource_type="content_task",
            resource_id=task.id,
            details_json=json.dumps(
                {
                    "artifact_count": len(artifact_ids),
                    "generation_run_count": len(generation_run_ids),
                    "video_project_deleted": project is not None,
                },
                ensure_ascii=False,
            ),
        )
    )

    # Remove the video-project graph before artifacts. Upload sessions point to
    # project assets, proposals point to revisions, and revisions self-reference.
    if project_asset_ids:
        session.execute(
            delete(MediaDerivative).where(MediaDerivative.project_asset_id.in_(project_asset_ids))
        )
    if upload_session_ids:
        session.execute(
            delete(VideoUploadChunk).where(
                VideoUploadChunk.upload_session_id.in_(upload_session_ids)
            )
        )
        session.execute(
            delete(VideoUploadSession).where(VideoUploadSession.id.in_(upload_session_ids))
        )
    if project_ids:
        session.execute(delete(AiEditProposal).where(AiEditProposal.project_id.in_(project_ids)))
        if project_asset_ids:
            session.execute(delete(ProjectAsset).where(ProjectAsset.id.in_(project_asset_ids)))
        # Detach the immutable revision DAG before deleting all its rows.
        for revision in revision_rows:
            revision.parent_revision_id = None
        session.flush()
        session.execute(
            delete(VideoProjectRevision).where(VideoProjectRevision.project_id.in_(project_ids))
        )
        session.execute(delete(VideoProject).where(VideoProject.id.in_(project_ids)))

    link_filter = (ArtifactLink.target_task_id == task.id) | (
        ArtifactLink.source_task_id == task.id
    )
    if artifact_ids:
        link_filter = link_filter | ArtifactLink.artifact_id.in_(artifact_ids)
    session.execute(delete(ArtifactLink).where(link_filter))
    session.execute(delete(TaskStage).where(TaskStage.task_id == task.id))
    session.execute(delete(EgressGrant).where(EgressGrant.task_id == task.id))

    job_filter = Job.task_id == task.id
    if generation_run_ids:
        job_filter = job_filter | Job.generation_run_id.in_(generation_run_ids)
    session.execute(delete(Job).where(job_filter))
    session.execute(delete(Artifact).where(Artifact.task_id == task.id))
    session.execute(delete(GenerationRun).where(GenerationRun.task_id == task.id))
    session.execute(delete(ContentTask).where(ContentTask.id == task.id))
