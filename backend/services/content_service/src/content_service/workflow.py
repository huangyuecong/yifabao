import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Artifact, ArtifactLink, AuditLog, ContentTask, GenerationRun, Job, TaskStage

STAGE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("configuration", "任务配置"),
    ("topic", "智能标题"),
    ("copy", "文案"),
    ("compliance", "合规检测"),
    ("image", "图片"),
    ("storyboard", "视频脚本"),
    ("voice_subtitles", "配音字幕"),
    ("video_preview", "视频生成"),
    ("export", "确认导出"),
)
STAGE_KEYS = tuple(item[0] for item in STAGE_DEFINITIONS)
STAGE_LABELS = dict(STAGE_DEFINITIONS)
STAGE_INDEX = {key: index for index, key in enumerate(STAGE_KEYS)}
MODE_STAGES = {
    # The complete workflow is presented as seven user-facing steps.  The two
    # legacy production stages remain valid internal capabilities (see
    # MODE_OPERABLE_STAGES) so old tasks and API integrations keep working.
    "full": frozenset(
        {
            "configuration",
            "topic",
            "copy",
            "compliance",
            "image",
            "video_preview",
            "export",
        }
    ),
    "copy": frozenset({"configuration", "topic", "copy", "compliance", "export"}),
    "image": frozenset({"configuration", "image", "export"}),
    "video": frozenset({"configuration", "video_preview", "export"}),
}
MODE_OPERABLE_STAGES = {
    **MODE_STAGES,
    "full": frozenset(STAGE_KEYS),
    # Preserve legacy production APIs and selections without adding steps to
    # the standalone editor's three-step navigation.
    "video": frozenset(
        {
            "configuration",
            "image",
            "storyboard",
            "voice_subtitles",
            "video_preview",
            "export",
        }
    ),
}
INITIAL_STAGE = {
    "full": "topic",
    "copy": "topic",
    "image": "image",
    "video": "video_preview",
}
SKIPPABLE_STAGES = frozenset({"topic"})
ARTIFACT_STAGE = {
    "topic": "topic",
    "copy": "copy",
    "compliance": "compliance",
    "image": "image",
    "storyboard": "storyboard",
    "voice": "voice_subtitles",
    "subtitle": "voice_subtitles",
    "ai_video_clip": "video_preview",
    "video": "video_preview",
}


class WorkflowConflictError(Exception):
    pass


class WorkflowValidationError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_mapping(value: str) -> dict[str, list[str]]:
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, item in loaded.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            result[key] = [item]
        elif isinstance(item, list):
            result[key] = list(dict.fromkeys(value for value in item if isinstance(value, str)))
    return result


def _artifact_content(artifact: Artifact | None) -> dict[str, Any]:
    if artifact is None:
        return {}
    try:
        loaded = json.loads(artifact.content_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _cancel_mismatched_subtitle_selection(
    session: Session,
    selections: dict[str, list[str]],
) -> bool:
    """Remove subtitles that were not generated from the selected voice.

    A subtitle timeline is a dependent result of one concrete voice artifact. When
    the voice changes, keeping the previous subtitle selected would allow a new
    audio file to be composed with an old timing track. This helper only repairs the
    current stage mapping. Artifact lifecycle is synchronized after the mapping is
    stored so an unreferenced historical subtitle returns to ``draft`` while a
    subtitle still selected or confirmed by another task keeps that global status.
    """

    subtitle_ids = selections.get("subtitle", [])
    if not subtitle_ids:
        return False
    voice_ids = selections.get("voice", [])
    selected_voice_id = voice_ids[0] if len(voice_ids) == 1 else None
    matching_subtitle_ids: list[str] = []
    for subtitle_id in subtitle_ids:
        subtitle = session.get(Artifact, subtitle_id)
        source_artifact_id = _artifact_content(subtitle).get("source_artifact_id")
        if selected_voice_id is not None and source_artifact_id == selected_voice_id:
            matching_subtitle_ids.append(subtitle_id)
    if matching_subtitle_ids:
        selections["subtitle"] = matching_subtitle_ids
    else:
        selections.pop("subtitle", None)
    return matching_subtitle_ids != subtitle_ids


def applicable_stages(task: ContentTask) -> frozenset[str]:
    return MODE_STAGES.get(task.mode, MODE_STAGES["full"])


def operable_stages(task: ContentTask) -> frozenset[str]:
    """Return stages accepted by internal/backward-compatible APIs.

    ``applicable_stages`` is the user-facing primary flow.  Keeping this
    separate lets legacy storyboard and voice/subtitle records remain usable
    without reintroducing them into the visible full or quick-video workflow.
    """

    return MODE_OPERABLE_STAGES.get(task.mode, MODE_OPERABLE_STAGES["full"])


def _normalize_current_stage(task: ContentTask) -> bool:
    merged_stages = {"storyboard", "voice_subtitles"}
    if task.mode == "video":
        merged_stages.add("image")
    if task.mode not in {"full", "video"} or task.current_stage not in merged_stages:
        return False
    task.current_stage = "video_preview"
    return True


def initialize_workflow(session: Session, task: ContentTask, user_id: str) -> list[TaskStage]:
    now = utcnow()
    applicable = applicable_stages(task)
    rows: list[TaskStage] = []
    for key in STAGE_KEYS:
        if key == "configuration":
            state = "confirmed"
        elif key not in applicable:
            state = "skipped"
        else:
            state = "not_started"
        row = TaskStage(
            task_id=task.id,
            stage_key=key,
            state=state,
            skipped_reason="当前入口不适用" if key not in applicable else None,
            confirmed_by=user_id if key == "configuration" else None,
            confirmed_at=now if key == "configuration" else None,
        )
        session.add(row)
        rows.append(row)
    task.current_stage = INITIAL_STAGE.get(task.mode, "topic")
    session.flush()
    return rows


def ensure_workflow(session: Session, task: ContentTask) -> list[TaskStage]:
    rows = list(
        session.scalars(
            select(TaskStage).where(TaskStage.task_id == task.id).order_by(TaskStage.created_at)
        ).all()
    )
    existing = {row.stage_key for row in rows}
    current_stage_normalized = _normalize_current_stage(task)
    if len(existing) == len(STAGE_KEYS):
        # Copy tasks created before the smart-title step was enabled already have
        # a topic row, but that row was stored as "not applicable". Reactivate
        # only that legacy system-skipped row; a title intentionally skipped by
        # a user has a different reason and must remain untouched.
        if task.mode == "copy":
            rows_by_key = {row.stage_key: row for row in rows}
            topic_stage = rows_by_key.get("topic")
            copy_stage = rows_by_key.get("copy")
            if (
                topic_stage is not None
                and topic_stage.state == "skipped"
                and topic_stage.skipped_reason == "当前入口不适用"
                and task.status == "draft"
                and task.current_stage == "copy"
                and copy_stage is not None
                and copy_stage.state == "not_started"
            ):
                topic_stage.state = "not_started"
                topic_stage.skipped_reason = None
                task.current_stage = "topic"
                session.flush()
                current_stage_normalized = False
        if current_stage_normalized:
            session.flush()
        return sorted(rows, key=lambda item: STAGE_INDEX[item.stage_key])
    applicable = applicable_stages(task)
    for key in STAGE_KEYS:
        if key in existing:
            continue
        state = "skipped" if key not in applicable else "not_started"
        row = TaskStage(
            task_id=task.id,
            stage_key=key,
            state=state,
            skipped_reason="当前入口不适用" if key not in applicable else None,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return sorted(rows, key=lambda item: STAGE_INDEX[item.stage_key])


def get_stage(session: Session, task: ContentTask, stage_key: str) -> TaskStage:
    if stage_key not in STAGE_INDEX:
        raise WorkflowValidationError("未知的内容生产阶段")
    ensure_workflow(session, task)
    stage = session.scalar(
        select(TaskStage).where(
            TaskStage.task_id == task.id,
            TaskStage.stage_key == stage_key,
        )
    )
    if stage is None:
        raise WorkflowValidationError("内容生产阶段不存在")
    if stage_key not in operable_stages(task):
        raise WorkflowValidationError("当前任务入口不包含该阶段")
    return stage


def require_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise WorkflowConflictError(f"版本冲突：当前版本为 {actual}，请求基于版本 {expected}")


def stage_json(task: ContentTask, stage: TaskStage) -> dict[str, Any]:
    applicable = applicable_stages(task)
    visible_order = [key for key in STAGE_KEYS if key in applicable]
    return {
        "key": stage.stage_key,
        "label": STAGE_LABELS[stage.stage_key],
        "order": (
            visible_order.index(stage.stage_key) + 1
            if stage.stage_key in applicable
            else STAGE_INDEX[stage.stage_key] + 1
        ),
        "applicable": stage.stage_key in applicable,
        "skippable": stage.stage_key in SKIPPABLE_STAGES,
        "state": stage.state,
        "requirements": stage.requirements,
        "selected_artifact_ids": _load_mapping(stage.selected_artifact_ids_json),
        "confirmed_artifact_ids": _load_mapping(stage.confirmed_artifact_ids_json),
        "skipped_reason": stage.skipped_reason,
        "version": stage.version,
        "confirmed_by": stage.confirmed_by,
        "confirmed_at": stage.confirmed_at,
        "updated_at": stage.updated_at,
    }


def workflow_json(session: Session, task: ContentTask) -> dict[str, Any]:
    from .export_selection import resolve_task_export_artifacts

    stages = ensure_workflow(session, task)
    return {
        "task_id": task.id,
        "task_version": task.version,
        "current_stage": task.current_stage,
        "stages": [stage_json(task, stage) for stage in stages],
        "export_artifact_ids": [item.id for item in resolve_task_export_artifacts(session, task)],
    }


def _artifact_stage_references(
    session: Session,
    artifact: Artifact,
    mapping_attribute: str,
) -> bool:
    """Return whether an artifact is in an active task-stage mapping.

    ``Artifact.status`` is a convenient denormalized display/index value, but
    older workflow revisions may have left more than one row marked
    ``confirmed``.  The task-stage mappings are the authoritative current
    selection.  Include target tasks which linked the artifact so a source
    artifact cannot be removed while another workflow is actively using it.
    """

    task_ids = {artifact.task_id}
    task_ids.update(
        session.scalars(
            select(ArtifactLink.target_task_id).where(
                ArtifactLink.artifact_id == artifact.id,
            )
        ).all()
    )
    stages = session.scalars(select(TaskStage).where(TaskStage.task_id.in_(task_ids))).all()
    return any(
        artifact.id in artifact_ids
        for stage in stages
        for artifact_ids in _load_mapping(getattr(stage, mapping_attribute, "{}") or "{}").values()
    )


def artifact_is_currently_selected(session: Session, artifact: Artifact) -> bool:
    return _artifact_stage_references(
        session,
        artifact,
        "selected_artifact_ids_json",
    )


def artifact_is_currently_confirmed(session: Session, artifact: Artifact) -> bool:
    return _artifact_stage_references(
        session,
        artifact,
        "confirmed_artifact_ids_json",
    )


def _synchronize_task_owned_stage_artifact_statuses(
    session: Session,
    task: ContentTask,
    stage: TaskStage,
    *,
    selected: dict[str, list[str]],
    confirmed: dict[str, list[str]],
    promote_draft_selections: bool = True,
) -> None:
    """Synchronize denormalized statuses with one stage's current mappings.

    Only artifacts owned by this task are candidates for change.  A task-owned
    artifact can also be linked into another task, so global stage references
    take precedence over the local stage being synchronized.  This preserves
    a source artifact required by a target workflow/export.
    """

    artifact_kinds = [
        kind for kind, stage_key in ARTIFACT_STAGE.items() if stage_key == stage.stage_key
    ]
    if not artifact_kinds:
        return
    selected_ids = {
        artifact_id for artifact_ids in selected.values() for artifact_id in artifact_ids
    }
    confirmed_ids = {
        artifact_id for artifact_ids in confirmed.values() for artifact_id in artifact_ids
    }
    artifacts = session.scalars(
        select(Artifact).where(
            Artifact.task_id == task.id,
            Artifact.kind.in_(artifact_kinds),
        )
    ).all()
    for artifact in artifacts:
        if artifact.status in {"discarded", "stale", "legacy_mock"}:
            continue
        globally_confirmed = artifact_is_currently_confirmed(session, artifact)
        globally_selected = artifact_is_currently_selected(session, artifact)
        if artifact.id in confirmed_ids or globally_confirmed:
            artifact.status = "confirmed"
        elif artifact.id in selected_ids or globally_selected:
            if promote_draft_selections or artifact.status != "draft":
                artifact.status = "selected"
        elif artifact.status in {"selected", "confirmed"}:
            artifact.status = "draft"


def activate_copy_selection(
    session: Session,
    task: ContentTask,
    stage: TaskStage,
    *,
    user_id: str,
    bump_stage_version: bool = False,
) -> Artifact | None:
    """Make the sole selected copy immediately ready for downstream use.

    Copy no longer has a separate user-confirmation action.  The stage selection
    remains the authority: only one explicitly selected, usable copy can become
    current.  This helper never searches historical artifacts, so a configuration
    change that cleared the selection cannot accidentally revive an old draft.
    Lazy legacy migration may advance only the stage version so stage-level CAS
    remains truthful without invalidating an already-open task editor.
    """

    if stage.stage_key != "copy" or stage.state == "stale":
        return None
    selected = _load_mapping(stage.selected_artifact_ids_json)
    copy_ids = selected.get("copy", [])
    if len(copy_ids) != 1:
        return None
    artifact = session.get(Artifact, copy_ids[0])
    if (
        artifact is None
        or artifact.kind != "copy"
        or artifact.status in {"discarded", "stale", "legacy_mock"}
        or bool(_artifact_content(artifact).get("legacy_mock"))
    ):
        return None
    confirmed = {"copy": [artifact.id]}
    readiness_changed = (
        stage.state != "confirmed"
        or _load_mapping(stage.confirmed_artifact_ids_json) != confirmed
        or artifact.status != "confirmed"
    )
    stage.confirmed_artifact_ids_json = _dump(confirmed)
    stage.state = "confirmed"
    if readiness_changed:
        stage.confirmed_by = user_id
        stage.confirmed_at = utcnow()
        if bump_stage_version:
            stage.version += 1
    stage.skipped_reason = None
    artifact.status = "confirmed"
    _synchronize_task_owned_stage_artifact_statuses(
        session,
        task,
        stage,
        selected=selected,
        confirmed=confirmed,
    )
    return artifact


def _invalidate_downstream(session: Session, task: ContentTask, stage_key: str) -> None:
    # Any upstream edit invalidates a previously completed task.  Completion
    # is only valid while the export-stage confirmation remains current.
    if task.status == "confirmed":
        task.status = "draft"
    for downstream in ensure_workflow(session, task):
        if STAGE_INDEX[downstream.stage_key] <= STAGE_INDEX[stage_key]:
            continue
        if downstream.stage_key not in operable_stages(task):
            continue
        if downstream.state in {"not_started", "skipped"}:
            continue
        selected = _load_mapping(downstream.selected_artifact_ids_json)
        confirmed = _load_mapping(downstream.confirmed_artifact_ids_json)
        if selected or confirmed:
            session.add(
                AuditLog(
                    owner_id=task.owner_id,
                    action="stage.selection_preserved",
                    resource_type="task_stage",
                    resource_id=downstream.id,
                    details_json=_dump(
                        {
                            "stage_key": downstream.stage_key,
                            "invalidated_by_stage": stage_key,
                            "state": downstream.state,
                            "selected_artifact_ids": selected,
                            "confirmed_artifact_ids": confirmed,
                        }
                    ),
                )
            )
        downstream.state = "stale"
        downstream.version += 1
        # Preserve artifacts as history, but never leave an old dependency
        # selection in the active workflow after an upstream choice changes.
        downstream.selected_artifact_ids_json = "{}"
        downstream.confirmed_artifact_ids_json = "{}"
        downstream.confirmed_by = None
        downstream.confirmed_at = None
        _synchronize_task_owned_stage_artifact_statuses(
            session,
            task,
            downstream,
            selected={},
            confirmed={},
        )


def update_requirements(
    session: Session,
    task: ContentTask,
    stage_key: str,
    requirements: str,
    expected_version: int,
) -> TaskStage:
    stage = get_stage(session, task, stage_key)
    require_version(stage.version, expected_version)
    changed = stage.requirements != requirements
    stage.requirements = requirements
    if changed:
        selected = _load_mapping(stage.selected_artifact_ids_json)
        stage.state = "stale" if selected else "not_started"
        stage.selected_artifact_ids_json = "{}"
        stage.confirmed_artifact_ids_json = "{}"
        stage.confirmed_by = None
        stage.confirmed_at = None
        _synchronize_task_owned_stage_artifact_statuses(
            session,
            task,
            stage,
            selected={},
            confirmed={},
        )
        stage.version += 1
        task.version += 1
        _invalidate_downstream(session, task, stage_key)
    return stage


def mark_configuration_changed(
    session: Session,
    task: ContentTask,
    user_id: str,
) -> TaskStage:
    stage = get_stage(session, task, "configuration")
    stage.state = "confirmed"
    stage.confirmed_by = user_id
    stage.confirmed_at = utcnow()
    task.status = "draft"
    task.current_stage = "configuration"
    stage.version += 1
    _invalidate_downstream(session, task, "configuration")
    return stage


def begin_generation_run(
    session: Session,
    task: ContentTask,
    stage_key: str,
    *,
    count: int,
    instruction: str,
    parameters: dict[str, Any],
    idempotency_key: str | None,
    user_id: str,
) -> tuple[GenerationRun, bool]:
    stage = get_stage(session, task, stage_key)
    if idempotency_key:
        existing = session.scalar(
            select(GenerationRun).where(
                GenerationRun.task_id == task.id,
                GenerationRun.stage_key == stage_key,
                GenerationRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing, True
    run = GenerationRun(
        task_id=task.id,
        stage_key=stage_key,
        status="running",
        requested_count=count,
        instruction=instruction,
        parameters_json=_dump(parameters),
        idempotency_key=idempotency_key,
        created_by=user_id,
    )
    # The preflight lookup above is only an optimisation.  Concurrent
    # requests can both pass it, so let the database unique constraint be the
    # final idempotency fence and recover the winner instead of surfacing a
    # 500 IntegrityError.
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        if idempotency_key:
            existing = session.scalar(
                select(GenerationRun).where(
                    GenerationRun.task_id == task.id,
                    GenerationRun.stage_key == stage_key,
                    GenerationRun.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing, True
        raise
    stage.state = "in_progress"
    stage.skipped_reason = None
    stage.version += 1
    task.current_stage = stage_key
    task.version += 1
    session.flush()
    return run, False


def attach_artifact(
    artifact: Artifact,
    run: GenerationRun,
    stage_key: str,
    candidate_index: int,
) -> None:
    artifact.stage_key = stage_key
    artifact.generation_run_id = run.id
    artifact.candidate_index = candidate_index


def complete_generation_run(
    session: Session,
    task: ContentTask,
    run: GenerationRun,
    artifacts: list[Artifact],
    *,
    provider_id: str | None = None,
    model: str | None = None,
    preserve_other_ai_video_shots: bool = False,
) -> TaskStage:
    stage = get_stage(session, task, run.stage_key)
    run.status = "succeeded"
    run.result_artifact_ids_json = _dump([artifact.id for artifact in artifacts])
    run.provider_id = provider_id
    run.model = model
    run.completed_at = utcnow()
    selections = _load_mapping(stage.selected_artifact_ids_json)
    previous = dict(selections)
    first_by_kind: dict[str, Artifact] = {}
    all_by_kind: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
        if artifact.status == "legacy_mock":
            continue
        first_by_kind.setdefault(artifact.kind, artifact)
        all_by_kind.setdefault(artifact.kind, []).append(artifact)
    for kind, artifact in first_by_kind.items():
        preserved: list[Artifact] = []
        incoming_shot_indexes = {
            item.candidate_index for item in all_by_kind[kind] if item.candidate_index is not None
        }
        for previous_id in selections.get(kind, []):
            previous_artifact = session.get(Artifact, previous_id)
            if (
                preserve_other_ai_video_shots
                and kind == "ai_video_clip"
                and previous_artifact is not None
                and previous_artifact.candidate_index not in incoming_shot_indexes
                and previous_artifact.status not in {"discarded", "legacy_mock"}
            ):
                preserved.append(previous_artifact)
            elif previous_artifact is not None and previous_artifact.status == "selected":
                previous_artifact.status = "draft"
        if kind == "ai_video_clip":
            merged = [*preserved, *all_by_kind[kind]]
            merged.sort(
                key=lambda item: (
                    item.candidate_index is None,
                    item.candidate_index if item.candidate_index is not None else 0,
                    item.created_at,
                )
            )
            selections[kind] = [item.id for item in merged]
        else:
            selections[kind] = [artifact.id]
    voice_changed = "voice" in first_by_kind and previous.get("voice", []) != selections.get(
        "voice", []
    )
    if voice_changed:
        _cancel_mismatched_subtitle_selection(session, selections)
    stage.selected_artifact_ids_json = _dump(selections)
    stage.confirmed_artifact_ids_json = "{}"
    stage.confirmed_by = None
    stage.confirmed_at = None
    stage.state = "awaiting_confirmation"
    _synchronize_task_owned_stage_artifact_statuses(
        session,
        task,
        stage,
        selected=selections,
        confirmed={},
        promote_draft_selections=False,
    )
    activate_copy_selection(
        session,
        task,
        stage,
        user_id=run.created_by,
    )
    stage.version += 1
    task.current_stage = run.stage_key
    task.version += 1
    if selections != previous:
        _invalidate_downstream(session, task, run.stage_key)
    return stage


def fail_generation_run(
    session: Session,
    task: ContentTask,
    run: GenerationRun,
    error: str,
) -> None:
    stage = get_stage(session, task, run.stage_key)
    run.status = "failed"
    run.error = error
    run.completed_at = utcnow()
    stage.state = "failed"
    stage.version += 1
    task.version += 1


def generation_run_json(
    session: Session,
    run: GenerationRun,
    artifact_serializer,
) -> dict[str, Any]:
    artifact_ids = json.loads(run.result_artifact_ids_json or "[]")
    artifacts = [
        artifact
        for artifact_id in artifact_ids
        if (artifact := session.get(Artifact, artifact_id)) is not None
    ]
    jobs = list(
        session.scalars(
            select(Job).where(Job.generation_run_id == run.id).order_by(Job.created_at)
        ).all()
    )
    return {
        "id": run.id,
        "task_id": run.task_id,
        "stage_key": run.stage_key,
        "status": run.status,
        "requested_count": run.requested_count,
        "instruction": run.instruction,
        "parameters": json.loads(run.parameters_json or "{}"),
        "artifact_ids": artifact_ids,
        "artifacts": [artifact_serializer(item) for item in artifacts],
        "job_id": jobs[0].id if jobs else None,
        "job_ids": [job.id for job in jobs],
        "provider_id": run.provider_id,
        "model": run.model,
        "idempotency_key": run.idempotency_key,
        "error": run.error,
        "created_by": run.created_by,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


def _select_artifacts(
    session: Session,
    task: ContentTask,
    stage: TaskStage,
    artifacts: list[Artifact],
    *,
    allow_empty: bool = False,
    replace_all: bool = False,
    selected_by: str | None = None,
) -> None:
    if not artifacts and not allow_empty:
        raise WorkflowValidationError("请至少选择一个候选内容")
    grouped: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
        expected_stage = ARTIFACT_STAGE.get(artifact.kind)
        linked_to_task = (
            artifact.task_id == task.id
            or session.scalar(
                select(ArtifactLink.id).where(
                    ArtifactLink.target_task_id == task.id,
                    ArtifactLink.artifact_id == artifact.id,
                    ArtifactLink.stage_key == stage.stage_key,
                )
            )
            is not None
        )
        if not linked_to_task or expected_stage != stage.stage_key:
            raise WorkflowValidationError("候选内容不属于当前任务阶段")
        if artifact.status in {"discarded", "stale", "legacy_mock"}:
            raise WorkflowConflictError("该候选内容已失效，不能选择")
        if artifact.stage_key is None:
            artifact.stage_key = expected_stage
        grouped.setdefault(artifact.kind, []).append(artifact)
    if stage.stage_key == "copy" and len(grouped.get("copy", [])) > 1:
        raise WorkflowValidationError("文案阶段只能选择一个当前版本")
    selections = _load_mapping(stage.selected_artifact_ids_json)
    previous = dict(selections)
    if replace_all:
        selections = {}
    for kind, kind_artifacts in grouped.items():
        new_ids = list(dict.fromkeys(item.id for item in kind_artifacts))
        selections[kind] = new_ids
        for artifact in kind_artifacts:
            if artifact.status == "draft":
                artifact.status = "selected"
    next_ids = {artifact_id for artifact_ids in selections.values() for artifact_id in artifact_ids}
    for previous_ids in previous.values():
        for previous_id in previous_ids:
            if previous_id in next_ids:
                continue
            old = session.get(Artifact, previous_id)
            if old is not None and old.status == "selected":
                old.status = "draft"
    # Validate the final mapping. With replace-all, a client can submit a new
    # voice together with a subtitle generated from the old voice; keep the
    # historical subtitle available, but do not leave that mismatched pair in
    # the current selection.
    if stage.stage_key == "voice_subtitles":
        _cancel_mismatched_subtitle_selection(session, selections)
    changed = previous != selections
    stage.selected_artifact_ids_json = _dump(selections)
    stage.confirmed_artifact_ids_json = "{}"
    stage.confirmed_by = None
    stage.confirmed_at = None
    stage.state = "awaiting_confirmation"
    stage.skipped_reason = None
    _synchronize_task_owned_stage_artifact_statuses(
        session,
        task,
        stage,
        selected=selections,
        confirmed={},
    )
    activate_copy_selection(
        session,
        task,
        stage,
        user_id=selected_by or (artifacts[0].created_by if artifacts else task.owner_id),
    )
    stage.version += 1
    task.current_stage = stage.stage_key
    task.version += 1
    if changed:
        _invalidate_downstream(session, task, stage.stage_key)


def select_artifacts(
    session: Session,
    task: ContentTask,
    stage_key: str,
    artifacts: list[Artifact],
    expected_version: int,
    user_id: str | None = None,
) -> TaskStage:
    stage = get_stage(session, task, stage_key)
    require_version(stage.version, expected_version)
    _select_artifacts(
        session,
        task,
        stage,
        artifacts,
        allow_empty=True,
        replace_all=True,
        selected_by=user_id,
    )
    task.workflow_enforced = True
    return stage


def adopt_revision(
    session: Session,
    task: ContentTask,
    artifact: Artifact,
    *,
    replace_all: bool = False,
) -> TaskStage | None:
    stage_key = ARTIFACT_STAGE.get(artifact.kind)
    if not stage_key or stage_key not in operable_stages(task):
        return None
    stage = get_stage(session, task, stage_key)
    _select_artifacts(
        session,
        task,
        stage,
        [artifact],
        replace_all=replace_all,
        selected_by=artifact.created_by,
    )
    return stage


def select_linked_artifacts(
    session: Session,
    task: ContentTask,
    artifacts: list[Artifact],
    *,
    user_id: str | None = None,
) -> list[TaskStage]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for artifact in artifacts:
        stage_key = ARTIFACT_STAGE.get(artifact.kind)
        if stage_key is None or stage_key not in operable_stages(task):
            raise WorkflowValidationError("存在不能加入目标任务的内容类型")
        if artifact.status != "confirmed":
            raise WorkflowConflictError("只有已确认的独立生成结果可以加入完整任务")
        grouped.setdefault(stage_key, {}).setdefault(artifact.kind, []).append(artifact.id)
    linked_copy_ids = list(dict.fromkeys(grouped.get("copy", {}).get("copy", [])))
    if len(linked_copy_ids) > 1:
        raise WorkflowValidationError("文案阶段只能选择一个当前版本")
    changed_stages: list[TaskStage] = []
    for stage_key, by_kind in grouped.items():
        stage = get_stage(session, task, stage_key)
        selections = _load_mapping(stage.selected_artifact_ids_json)
        previous_voice_ids = list(selections.get("voice", []))
        changed = False
        for kind, artifact_ids in by_kind.items():
            normalized = list(dict.fromkeys(artifact_ids))
            if selections.get(kind) != normalized:
                selections[kind] = normalized
                changed = True
        if (
            "voice" in by_kind
            and previous_voice_ids != selections.get("voice", [])
            and _cancel_mismatched_subtitle_selection(session, selections)
        ):
            changed = True
        if not changed:
            continue
        stage.selected_artifact_ids_json = _dump(selections)
        stage.confirmed_artifact_ids_json = "{}"
        stage.confirmed_by = None
        stage.confirmed_at = None
        stage.state = "awaiting_confirmation"
        _synchronize_task_owned_stage_artifact_statuses(
            session,
            task,
            stage,
            selected=selections,
            confirmed={},
        )
        activate_copy_selection(
            session,
            task,
            stage,
            user_id=user_id or task.owner_id,
        )
        stage.version += 1
        task.version += 1
        task.current_stage = stage_key
        task.workflow_enforced = True
        _invalidate_downstream(session, task, stage_key)
        changed_stages.append(stage)
    return changed_stages


def _export_reconfirmation_blockers(
    session: Session,
    task: ContentTask,
) -> list[TaskStage]:
    """Return applicable upstream stages not ready for a stale export refresh."""

    required_final_stage = {
        "image": "image",
        "copy": "compliance",
        "video": "video_preview",
        "full": "video_preview",
    }.get(task.mode, "video_preview")
    blockers: list[TaskStage] = []
    for candidate in ensure_workflow(session, task):
        if candidate.stage_key == "export" or candidate.stage_key not in applicable_stages(task):
            continue
        if candidate.stage_key == required_final_stage:
            if candidate.state != "confirmed":
                blockers.append(candidate)
            continue
        explicitly_skipped = (
            candidate.state == "skipped"
            and candidate.stage_key in SKIPPABLE_STAGES
            and bool((candidate.skipped_reason or "").strip())
            and candidate.skipped_reason != "当前入口不适用"
        )
        # Preserve compatibility with untouched optional upstream steps, while
        # still blocking a visible stage that was entered and left unresolved.
        # Hidden legacy stages were excluded above without changing their data.
        if candidate.state not in {"confirmed", "not_started"} and not explicitly_skipped:
            blockers.append(candidate)
    return blockers


def confirm_stage(
    session: Session,
    task: ContentTask,
    stage_key: str,
    expected_version: int,
    user_id: str,
    artifacts: list[Artifact] | None = None,
) -> TaskStage:
    stage = get_stage(session, task, stage_key)
    require_version(stage.version, expected_version)
    if stage.state == "stale":
        if stage_key != "export":
            raise WorkflowConflictError(
                "上游内容已变化，请先通过选择接口重新选择或重新生成后再确认"
            )
        # Export has no candidates of its own. Its stale state is recovered by
        # acknowledging the newly confirmed upstream result, but never while an
        # applicable production stage is still awaiting work. Hidden legacy
        # stages are intentionally excluded through ``applicable_stages``.
        blockers = _export_reconfirmation_blockers(session, task)
        if blockers:
            labels = "、".join(STAGE_LABELS[item.stage_key] for item in blockers)
            raise WorkflowConflictError(
                f"上游阶段尚未完成或重新确认：{labels}；请先处理这些阶段后再确认导出"
            )
    if artifacts is not None:
        _select_artifacts(session, task, stage, artifacts, selected_by=user_id)
    selections = _load_mapping(stage.selected_artifact_ids_json)
    if stage_key not in {"configuration", "compliance", "export"} and not selections:
        raise WorkflowConflictError("请先选择该阶段的候选内容")
    if stage_key == "voice_subtitles":
        voice_ids = selections.get("voice", [])
        subtitle_ids = selections.get("subtitle", [])
        if len(voice_ids) != 1 or len(subtitle_ids) != 1:
            raise WorkflowConflictError(
                "请先选择一组相互匹配的配音和字幕版本；重新选择配音后，"
                "需重新生成或选择由该配音生成的字幕"
            )
        voice = session.get(Artifact, voice_ids[0])
        subtitle = session.get(Artifact, subtitle_ids[0])
        if voice is None or subtitle is None:
            raise WorkflowConflictError("选中的配音或字幕版本不存在，请重新选择")
        subtitle_source_id = _artifact_content(subtitle).get("source_artifact_id")
        if subtitle_source_id != voice_ids[0]:
            raise WorkflowConflictError(
                "当前字幕并非由当前选中的配音生成，请根据该配音重新生成字幕，"
                "或选择与该配音匹配的字幕版本"
            )
    for artifact_ids in selections.values():
        for artifact_id in artifact_ids:
            selected = session.get(Artifact, artifact_id)
            if selected is None or selected.status in {"discarded", "stale", "legacy_mock"}:
                raise WorkflowConflictError("已选择的候选内容不存在或已经失效")
            selected.status = "confirmed"
    stage.confirmed_artifact_ids_json = _dump(selections)
    _synchronize_task_owned_stage_artifact_statuses(
        session,
        task,
        stage,
        selected=selections,
        confirmed=selections,
    )
    stage.state = "confirmed"
    stage.confirmed_by = user_id
    stage.confirmed_at = utcnow()
    stage.skipped_reason = None
    stage.version += 1
    task.version += 1
    task.workflow_enforced = True
    if stage_key == "export":
        task.status = "confirmed"
        task.current_stage = "export"
    else:
        following = [
            key
            for key in STAGE_KEYS[STAGE_INDEX[stage_key] + 1 :]
            if key in applicable_stages(task)
        ]
        task.current_stage = following[0] if following else stage_key
    return stage


def skip_stage(
    session: Session,
    task: ContentTask,
    stage_key: str,
    expected_version: int,
    reason: str,
) -> TaskStage:
    stage = get_stage(session, task, stage_key)
    require_version(stage.version, expected_version)
    if stage_key not in SKIPPABLE_STAGES:
        raise WorkflowValidationError("该阶段不能跳过")
    stage.state = "skipped"
    stage.skipped_reason = reason.strip() or "用户选择跳过"
    stage.selected_artifact_ids_json = "{}"
    stage.confirmed_artifact_ids_json = "{}"
    stage.confirmed_by = None
    stage.confirmed_at = None
    _synchronize_task_owned_stage_artifact_statuses(
        session,
        task,
        stage,
        selected={},
        confirmed={},
    )
    stage.version += 1
    task.version += 1
    task.workflow_enforced = True
    _invalidate_downstream(session, task, stage_key)
    following = [
        key for key in STAGE_KEYS[STAGE_INDEX[stage_key] + 1 :] if key in applicable_stages(task)
    ]
    task.current_stage = following[0] if following else stage_key
    return stage


def confirmed_selected_artifacts(
    session: Session,
    task: ContentTask,
    *,
    applicable_only: bool = False,
) -> list[Artifact]:
    artifact_ids: list[str] = []
    for stage in ensure_workflow(session, task):
        if applicable_only and stage.stage_key not in applicable_stages(task):
            continue
        if stage.state != "confirmed":
            continue
        for selected_ids in _load_mapping(stage.confirmed_artifact_ids_json).values():
            for artifact_id in selected_ids:
                if artifact_id not in artifact_ids:
                    artifact_ids.append(artifact_id)
    return [
        artifact
        for artifact_id in artifact_ids
        if (artifact := session.get(Artifact, artifact_id)) is not None
        and artifact.status == "confirmed"
        and not bool(json.loads(artifact.content_json or "{}").get("legacy_mock"))
    ]
