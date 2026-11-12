"""Resolve user-selected deliverables without reviving the active workflow."""

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Artifact, ArtifactLink, AuditLog, ContentTask, GenerationRun, TaskStage

_DELIVERABLE_KINDS = {"topic", "copy", "image", "voice", "subtitle", "video"}
_UNUSABLE_STATUSES = {"discarded", "stale", "legacy_mock"}


def _json(value: str | None, default):
    try:
        return json.loads(value or "null") or default
    except (TypeError, ValueError):
        return default


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def resolve_task_export_artifacts(session: Session, task: ContentTask) -> list[Artifact]:
    """Use live confirmations, or an archived selection with explicit provenance.

    Historical recovery is confined to completed video tasks. Configuration edits
    may clear workflow readiness, but must not erase the user's deliverables.
    Successful generation runs record their auto-selected first candidate; audit
    events record subsequent choices and edits. No arbitrary latest draft is used.
    """
    from .workflow import ARTIFACT_STAGE, STAGE_INDEX, applicable_stages, operable_stages

    # Quick-video navigation has three steps, but previously confirmed images,
    # voices and subtitles remain deliverables in existing task packages.
    applicable = operable_stages(task) if task.mode == "video" else applicable_stages(task)
    stages = list(session.scalars(select(TaskStage).where(TaskStage.task_id == task.id)))
    stage_by_key = {item.stage_key: item for item in stages}
    stage_by_id = {item.id: item.stage_key for item in stages}
    links = list(
        session.scalars(select(ArtifactLink).where(ArtifactLink.target_task_id == task.id))
    )
    linked_stages = {item.artifact_id: item.stage_key for item in links}
    artifacts = list(
        session.scalars(
            select(Artifact).where((Artifact.task_id == task.id) | Artifact.id.in_(linked_stages))
        )
    )
    by_id = {item.id: item for item in artifacts}

    def usable(artifact_id: str, *, cutoff: datetime | None = None) -> Artifact | None:
        item = by_id.get(artifact_id)
        if item is None or item.kind not in _DELIVERABLE_KINDS:
            return None
        if item.status in _UNUSABLE_STATUSES or _json(item.content_json, {}).get("legacy_mock"):
            return None
        if item.task_id != task.id and linked_stages.get(item.id) != ARTIFACT_STAGE.get(item.kind):
            return None
        if cutoff and item.created_at and _utc(item.created_at) > _utc(cutoff):
            return None
        return item

    def unique(ids, *, cutoff: datetime | None = None) -> list[Artifact]:
        result = []
        seen = set()
        for artifact_id in ids:
            if not isinstance(artifact_id, str) or artifact_id in seen:
                continue
            item = usable(artifact_id, cutoff=cutoff)
            if item is not None:
                seen.add(artifact_id)
                result.append(item)
        return result

    if task.mode not in {"full", "video"} and not task.workflow_enforced:
        return unique(item.id for item in artifacts if item.status == "confirmed")

    current_ids = []
    for stage in stages:
        if stage.stage_key not in applicable or stage.state != "confirmed":
            continue
        mapping = _json(stage.confirmed_artifact_ids_json, {})
        if not isinstance(mapping, dict):
            continue
        for ids in mapping.values():
            if isinstance(ids, list):
                current_ids.extend(
                    item
                    for item in ids
                    if isinstance(item, str) and item in by_id and by_id[item].status == "confirmed"
                )
    current = unique(current_ids)
    archived = task.archived_at is not None or task.status == "archived"
    export_stage = stage_by_key.get("export")
    completed_export = bool(
        task.status == "confirmed"
        and export_stage is not None
        and export_stage.state == "confirmed"
        and export_stage.confirmed_at is not None
    )
    final = next((item for item in reversed(current) if item.kind == "video"), None)
    if not (archived or completed_export) or final is None or task.mode not in {"full", "video"}:
        return current
    # Restoring a finished task only unlocks editing. Its existing completed
    # export still identifies the same deliverables until an actual edit occurs.
    exported_at = task.archived_at or (export_stage.confirmed_at if export_stage else None)

    content = _json(final.content_json, {})
    snapshot_archive = content.get("deliverable_selection_archived_at")
    try:
        same_archive = bool(task.archived_at) and _utc(
            datetime.fromisoformat(snapshot_archive)
        ) == _utc(task.archived_at)
    except (TypeError, ValueError):
        same_archive = False
    snapshot = content.get("deliverable_artifact_ids")
    snapshot_stage_version = content.get("deliverable_export_stage_version")
    if isinstance(snapshot_stage_version, int) and not isinstance(snapshot_stage_version, bool):
        same_archive = bool(
            export_stage is not None
            and export_stage.state == "confirmed"
            and snapshot_stage_version == export_stage.version
        )
    if (
        isinstance(snapshot, list)
        and content.get("deliverable_task_id") == task.id
        and same_archive
    ):
        return unique([*snapshot, final.id], cutoff=exported_at)

    cutoff = min(
        (value for value in (exported_at, final.created_at) if value is not None),
        key=_utc,
    )
    known_ids = [task.id, *stage_by_id, *by_id]
    audits = session.scalars(
        select(AuditLog).where(AuditLog.resource_id.in_(known_ids), AuditLog.created_at <= cutoff)
    ).all()
    events = [
        (
            _utc(item.created_at),
            item.id,
            item.action,
            item.resource_id,
            _json(item.details_json, {}),
        )
        for item in audits
    ]
    for run in session.scalars(
        select(GenerationRun).where(
            GenerationRun.task_id == task.id,
            GenerationRun.status == "succeeded",
            GenerationRun.completed_at <= cutoff,
        )
    ):
        events.append(
            (
                _utc(run.completed_at),
                run.id,
                "generation.selected",
                run.stage_key,
                {"artifact_ids": _json(run.result_artifact_ids_json, [])},
            )
        )
    events.sort(key=lambda item: (item[0], item[1]))
    choices: dict[str, dict[str, list[str]]] = {}
    preserved: dict[str, dict[str, list[str]]] = {}
    explicitly_empty: set[str] = set()

    def clear_downstream(stage_key: str) -> None:
        for downstream in list(choices.keys() | preserved.keys()):
            if STAGE_INDEX.get(downstream, -1) > STAGE_INDEX.get(stage_key, -1):
                choices.pop(downstream, None)
                preserved.pop(downstream, None)

    def select_ids(stage_key: str, ids: list, *, first_only: bool = False) -> None:
        mapping: dict[str, list[str]] = {}
        for artifact_id in ids:
            item = by_id.get(artifact_id) if isinstance(artifact_id, str) else None
            if item is None or ARTIFACT_STAGE.get(item.kind) != stage_key:
                continue
            if first_only and item.kind in mapping:
                continue
            mapping.setdefault(item.kind, []).append(item.id)
        choices[stage_key] = mapping
        preserved.pop(stage_key, None)
        if mapping:
            explicitly_empty.discard(stage_key)
        else:
            explicitly_empty.add(stage_key)
        clear_downstream(stage_key)

    for _, _, action, resource_id, details in events:
        if not isinstance(details, dict):
            continue
        stage_key = stage_by_id.get(resource_id)
        if action == "generation.selected":
            select_ids(resource_id, details.get("artifact_ids", []), first_only=True)
        elif action == "stage.candidates_selected" and stage_key:
            ids = details.get("artifact_ids")
            if isinstance(ids, list):
                select_ids(stage_key, ids)
        elif action in {"artifact.revised", "artifact.confirmed"}:
            item = by_id.get(resource_id)
            if item is not None and item.task_id == task.id and ARTIFACT_STAGE.get(item.kind):
                select_ids(ARTIFACT_STAGE[item.kind], [item.id])
        elif action in {"stage.skipped", "stage.requirements_updated"} and stage_key:
            select_ids(stage_key, [])
        elif action == "stage.selection_preserved" and stage_key:
            choices.pop(stage_key, None)
            if details.get("invalidated_by_stage") == "configuration":
                mapping = details.get("confirmed_artifact_ids") or details.get(
                    "selected_artifact_ids"
                )
                if isinstance(mapping, dict) and mapping and stage_key not in explicitly_empty:
                    preserved[stage_key] = mapping
            else:
                preserved.pop(stage_key, None)
        elif action == "task.updated" and resource_id == task.id:
            # Legacy task updates did not store the previous selection. Replay
            # known selections exactly, retaining them across repeated saves.
            for key, mapping in choices.items():
                if mapping and key not in explicitly_empty:
                    preserved[key] = mapping
            choices.clear()
        elif action == "stage.confirmed" and stage_key == "compliance":
            # Old auto-current copy edits can also be proved by the copy on
            # which the user explicitly accepted the compliance result.
            for compliance_id in choices.get("compliance", {}).get("compliance", []):
                compliance = by_id.get(compliance_id)
                source_id = (
                    _json(compliance.content_json, {}).get("source_artifact_id")
                    if compliance
                    else None
                )
                copy = by_id.get(source_id)
                if (
                    copy
                    and copy.kind == "copy"
                    and "copy" not in explicitly_empty
                    and not choices.get("copy")
                ):
                    choices["copy"] = {"copy": [copy.id]}

    recovered_ids = []
    for key in sorted(applicable, key=lambda item: STAGE_INDEX[item]):
        stage = stage_by_key.get(key)
        if (
            stage is None
            or stage.state != "stale"
            or key in explicitly_empty
            or _json(stage.selected_artifact_ids_json, {})
            or _json(stage.confirmed_artifact_ids_json, {})
        ):
            continue
        for ids in preserved.get(key, {}).values():
            if isinstance(ids, list):
                recovered_ids.extend(ids)
    return unique([*current_ids, *recovered_ids], cutoff=exported_at)
