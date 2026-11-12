"""Copy archived work into private, unpublished editing drafts.

Lifecycle events live in the existing append-only audit log. Workflow status is
deliberately independent: saving/generating cannot accidentally publish a draft.
Media files are immutable shared inputs; only database identities are copied.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from .export_selection import resolve_task_export_artifacts
from .models import (
    Artifact,
    ArtifactLink,
    AuditLog,
    ContentTask,
    GenerationRun,
    Job,
    MediaDerivative,
    ProjectAsset,
    TaskStage,
    VideoProject,
    VideoProjectRevision,
)
from .workflow import ARTIFACT_STAGE

CREATED = "task.edit_draft.created"
PROMOTED = "task.edit_draft.promoted"


def legacy_archived_task_ids():
    completed = select(TaskStage.task_id).where(
        TaskStage.stage_key == "export",
        TaskStage.state == "confirmed",
        TaskStage.confirmed_at.is_not(None),
    )
    active = select(Job.task_id).where(Job.status.in_(["queued", "running", "retrying"]))
    return select(ContentTask.id).where(
        ContentTask.status == "confirmed",
        ContentTask.id.in_(completed),
        ContentTask.id.not_in(active),
    )


def archive_snapshot_time(task: ContentTask, session: Session | None = None):
    """Recognize exact old export confirmations without backfilling user data."""
    if task.archived_at is not None:
        return task.archived_at
    session = session or object_session(task)
    if session is None or task.status != "confirmed":
        return None
    if session.scalar(
        select(ContentTask.id).where(
            ContentTask.id == task.id,
            ContentTask.id.in_(legacy_archived_task_ids()),
        )
    ):
        return session.scalar(
            select(TaskStage.confirmed_at).where(
                TaskStage.task_id == task.id,
                TaskStage.stage_key == "export",
            )
        )
    return None


def is_archived_snapshot(task: ContentTask, session: Session | None = None) -> bool:
    return task.status == "archived" or archive_snapshot_time(task, session) is not None


def hidden_edit_draft_ids():
    published = select(AuditLog.resource_id).where(
        AuditLog.resource_type == "content_task",
        AuditLog.action == PROMOTED,
    )
    return select(AuditLog.resource_id).where(
        AuditLog.resource_type == "content_task",
        AuditLog.action == CREATED,
        AuditLog.resource_id.not_in(published),
    )


def edit_draft_fields(session: Session | None, task_id: str) -> dict:
    result = {"is_edit_draft": False, "source_task_id": None}
    if session is None:
        return result
    events = list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.resource_type == "content_task",
                AuditLog.resource_id == task_id,
                AuditLog.action.in_([CREATED, PROMOTED]),
            )
        )
    )
    created = next((event for event in events if event.action == CREATED), None)
    if created is not None:
        result["source_task_id"] = json.loads(created.details_json).get("source_task_id")
        result["is_edit_draft"] = not any(event.action == PROMOTED for event in events)
    return result


def promote_edit_draft(session: Session, task: ContentTask, user_id: str) -> None:
    fields = edit_draft_fields(session, task.id)
    if fields["is_edit_draft"]:
        session.add(
            AuditLog(
                owner_id=user_id,
                action=PROMOTED,
                resource_type="content_task",
                resource_id=task.id,
                details_json=json.dumps({"source_task_id": fields["source_task_id"]}),
            )
        )


def _values(row, **changes) -> dict:
    return {**{column.key: getattr(row, column.key) for column in row.__table__.columns}, **changes}


_REFERENCE_KEYS = {
    "id",
    "task_id",
    "source_task_id",
    "target_task_id",
    "deliverable_task_id",
    "project_id",
    "project_asset_id",
    "asset_id",
    "source_id",
    "source_ids",
    "revision_id",
    "base_revision_id",
    "candidate_revision_id",
    "parent_revision_id",
    "current_revision_id",
    "generation_run_id",
    "parent_id",
    "reference_ids",
}


def _remap(value, identities: dict[str, str], *, reference: bool = False):
    # Do not replace UUID substrings in prompts, URLs, storage paths or external
    # provider provenance. Only exact, explicitly structured local references.
    if isinstance(value, dict):
        return {
            key: _remap(
                item,
                identities,
                reference=(
                    reference
                    or key in _REFERENCE_KEYS
                    or key.endswith(("artifact_id", "artifact_ids", "asset_id", "asset_ids"))
                ),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_remap(item, identities, reference=reference) for item in value]
    return identities.get(value, value) if reference and isinstance(value, str) else value


def _remap_json(value: str, identities: dict[str, str], *, reference: bool = False) -> str:
    return json.dumps(
        _remap(json.loads(value), identities, reference=reference), ensure_ascii=False
    )


def restore_archive_as_draft(session: Session, source: ContentTask, user_id: str) -> ContentTask:
    """Return/resume a separate editing copy without modifying the archive.

    The caller owns access checks and the transaction. Locking the source row
    serializes concurrent restores on databases which support row-level locks.
    """
    session.scalar(select(ContentTask).where(ContentTask.id == source.id).with_for_update())
    existing = session.scalars(
        select(ContentTask)
        .where(
            ContentTask.owner_id == user_id,
            ContentTask.id.in_(hidden_edit_draft_ids()),
            ContentTask.archived_at.is_(None),
            ContentTask.status != "archived",
        )
        .order_by(ContentTask.created_at.desc())
    ).all()
    for candidate in existing:
        if edit_draft_fields(session, candidate.id)["source_task_id"] == source.id:
            return candidate

    # Resolve before copying: legacy archives can preserve the exact title/copy/
    # image selection in audit history rather than their current stage fields.
    deliverables = resolve_task_export_artifacts(session, source)
    stages = list(session.scalars(select(TaskStage).where(TaskStage.task_id == source.id)))
    links = list(
        session.scalars(select(ArtifactLink).where(ArtifactLink.target_task_id == source.id))
    )
    project = session.scalar(select(VideoProject).where(VideoProject.task_id == source.id))
    project_assets = (
        list(
            session.scalars(
                select(ProjectAsset).where(
                    ProjectAsset.project_id == project.id,
                )
            )
        )
        if project
        else []
    )
    referenced_artifacts = {item.artifact_id for item in links}
    referenced_artifacts.update(item.id for item in deliverables)
    referenced_artifacts.update(item.artifact_id for item in project_assets if item.artifact_id)
    artifacts = list(
        session.scalars(
            select(Artifact).where(
                (Artifact.task_id == source.id) | Artifact.id.in_(referenced_artifacts),
            )
        )
    )
    runs = list(session.scalars(select(GenerationRun).where(GenerationRun.task_id == source.id)))
    revisions = (
        list(
            session.scalars(
                select(VideoProjectRevision).where(
                    VideoProjectRevision.project_id == project.id,
                )
            )
        )
        if project
        else []
    )
    derivatives = (
        list(
            session.scalars(
                select(MediaDerivative).where(
                    MediaDerivative.project_asset_id.in_([item.id for item in project_assets]),
                    MediaDerivative.status == "ready",
                )
            )
        )
        if project_assets
        else []
    )
    identities = {
        item.id: str(uuid4())
        for item in [source, *stages, *artifacts, *runs, *revisions, *project_assets, *derivatives]
    }
    if project:
        identities[project.id] = str(uuid4())
    now = datetime.now(UTC)
    task = ContentTask(
        **_values(
            source,
            id=identities[source.id],
            owner_id=user_id,
            status="draft",
            archived_at=None,
            archived_workflow_status=None,
            version=1,
            allowed_users_json="[]",
            allowed_departments_json="[]",
            created_at=now,
            updated_at=now,
        )
    )
    session.add(task)
    session.flush()
    for run in runs:
        terminal = run.status in {"succeeded", "failed", "cancelled"}
        session.add(
            GenerationRun(
                **_values(
                    run,
                    id=identities[run.id],
                    task_id=task.id,
                    status=run.status if terminal else "cancelled",
                    error=run.error
                    if terminal
                    else "已从归档恢复为独立草稿，原生成任务不会重复执行。",
                    completed_at=run.completed_at if terminal else now,
                    parameters_json=_remap_json(run.parameters_json, identities),
                    result_artifact_ids_json=_remap_json(
                        run.result_artifact_ids_json, identities, reference=True
                    ),
                )
            )
        )
    session.flush()
    cloned_artifacts = {}
    for artifact in artifacts:
        content = _remap(json.loads(artifact.content_json), identities)
        # A new draft has no export confirmation yet. The selected content below
        # carries membership until its own final confirmation freezes a snapshot.
        for field in ("deliverable_selection_archived_at", "deliverable_export_stage_version"):
            content.pop(field, None)
        copied = Artifact(
            **_values(
                artifact,
                id=identities[artifact.id],
                task_id=task.id,
                generation_run_id=identities.get(artifact.generation_run_id),
                parent_id=identities.get(artifact.parent_id),
                content_json=json.dumps(content, ensure_ascii=False),
            )
        )
        cloned_artifacts[artifact.id] = copied
        session.add(copied)
    session.flush()
    preserved = {}
    for artifact in deliverables:
        key = ARTIFACT_STAGE.get(artifact.kind)
        if key:
            preserved.setdefault(key, {}).setdefault(artifact.kind, []).append(
                identities[artifact.id]
            )
            cloned_artifacts[artifact.id].status = "confirmed"
    for stage in stages:
        values = _values(
            stage,
            id=identities[stage.id],
            task_id=task.id,
            selected_artifact_ids_json=_remap_json(
                stage.selected_artifact_ids_json, identities, reference=True
            ),
            confirmed_artifact_ids_json=_remap_json(
                stage.confirmed_artifact_ids_json, identities, reference=True
            ),
        )
        if stage.stage_key in preserved:
            values.update(
                selected_artifact_ids_json=json.dumps(preserved[stage.stage_key]),
                confirmed_artifact_ids_json=json.dumps(preserved[stage.stage_key]),
                state="confirmed",
            )
        if stage.stage_key == "export":
            values.update(
                state="in_progress",
                confirmed_at=None,
                confirmed_by=None,
                selected_artifact_ids_json="{}",
                confirmed_artifact_ids_json="{}",
            )
        session.add(TaskStage(**values))
    if project:
        copied_project = VideoProject(
            **_values(
                project,
                id=identities[project.id],
                task_id=task.id,
                current_revision_id=identities[project.current_revision_id],
                created_by=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(copied_project)
        session.flush()
        # Insert revisions without parent links first, then reattach the DAG;
        # unordered historical branches must also satisfy foreign-key checks.
        cloned_revisions = []
        for revision in revisions:
            copied = VideoProjectRevision(
                **_values(
                    revision,
                    id=identities[revision.id],
                    project_id=copied_project.id,
                    parent_revision_id=None,
                    timeline_json=_remap_json(revision.timeline_json, identities),
                    operations_json=_remap_json(revision.operations_json, identities),
                )
            )
            session.add(copied)
            cloned_revisions.append((revision, copied))
        session.flush()
        for original, copied in cloned_revisions:
            copied.parent_revision_id = identities.get(original.parent_revision_id)
        copied_project.current_revision_id = identities.get(project.current_revision_id)
        for asset in project_assets:
            session.add(
                ProjectAsset(
                    **_values(
                        asset,
                        id=identities[asset.id],
                        project_id=copied_project.id,
                        artifact_id=identities.get(asset.artifact_id),
                        source_id=identities.get(asset.source_id, asset.source_id),
                        metadata_json=_remap_json(asset.metadata_json, identities),
                    )
                )
            )
        session.flush()
        for derivative in derivatives:
            session.add(
                MediaDerivative(
                    **_values(
                        derivative,
                        id=identities[derivative.id],
                        project_asset_id=identities[derivative.project_asset_id],
                        metadata_json=_remap_json(derivative.metadata_json, identities),
                    )
                )
            )
    session.add(
        AuditLog(
            owner_id=user_id,
            action=CREATED,
            resource_type="content_task",
            resource_id=task.id,
            details_json=json.dumps(
                {"source_task_id": source.id, "source_task_version": source.version}
            ),
        )
    )
    session.flush()
    return task
