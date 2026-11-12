from datetime import UTC, datetime, timedelta
from uuid import uuid4


def _setup(session):
    from content_service.models import Artifact, ContentTask
    from content_service.services import dump
    from content_service.workflow import ensure_workflow

    start = datetime.now(UTC) - timedelta(minutes=5)
    task = ContentTask(
        id=str(uuid4()), owner_id="user-a", mode="full", name="导出选择回归", workflow_enforced=True
    )
    session.add(task)
    session.flush()
    stages = {item.stage_key: item for item in ensure_workflow(session, task)}

    def add(kind, version=1, **content):
        item = Artifact(
            id=str(uuid4()),
            task_id=task.id,
            kind=kind,
            version=version,
            status="draft",
            content_json=dump(content),
            created_by="user-a",
            created_at=start,
        )
        session.add(item)
        session.flush()
        return item

    final = add("video", purpose="final_project_render")
    final.status = "confirmed"
    final.created_at = start + timedelta(seconds=100)
    stages["video_preview"].state = "confirmed"
    stages["video_preview"].selected_artifact_ids_json = dump({"video": [final.id]})
    stages["video_preview"].confirmed_artifact_ids_json = dump({"video": [final.id]})
    for key in ("topic", "copy", "compliance", "image"):
        stages[key].state = "stale"
    task.status = "confirmed"
    task.archived_at = start + timedelta(seconds=110)
    session.flush()
    return task, stages, final, add, start


def _audit(session, task, action, resource_id, at, **details):
    from content_service.models import AuditLog
    from content_service.services import dump

    session.add(
        AuditLog(
            owner_id=task.owner_id,
            action=action,
            resource_type="test",
            resource_id=resource_id,
            details_json=dump(details),
            created_at=at,
        )
    )


def test_quick_video_three_steps_keep_confirmed_legacy_deliverables(content_client):
    from content_service.db import SessionLocal
    from content_service.export_selection import resolve_task_export_artifacts
    from content_service.services import dump
    from content_service.workflow import applicable_stages

    with SessionLocal() as session:
        task, stages, final, add, _ = _setup(session)
        task.mode = "video"
        task.archived_at = None
        task.status = "draft"
        expected = {final.id}
        for key, kinds in [("image", ["image"]), ("voice_subtitles", ["voice", "subtitle"])]:
            mapping = {}
            for kind in kinds:
                artifact = add(kind)
                artifact.status = "confirmed"
                mapping[kind] = [artifact.id]
                expected.add(artifact.id)
            stages[key].state = "confirmed"
            stages[key].selected_artifact_ids_json = dump(mapping)
            stages[key].confirmed_artifact_ids_json = dump(mapping)
        session.flush()
        assert applicable_stages(task) == {"configuration", "video_preview", "export"}
        assert {item.id for item in resolve_task_export_artifacts(session, task)} == expected


def test_archived_export_recovers_exact_legacy_selections_and_copy_revision(content_client):
    from content_service.db import SessionLocal
    from content_service.export_selection import resolve_task_export_artifacts

    with SessionLocal() as session:
        task, stages, final, add, start = _setup(session)
        topic = add("topic", 4)
        old_copy = add("copy", 2, source_title_artifact_id=topic.id)
        copy = add("copy", 5, source_title_artifact_id=topic.id)
        image2, image3 = add("image", 2), add("image", 3)
        add("copy", 6)  # A newer unselected draft must never be substituted.
        add("image", 4)
        events = [
            ("stage.candidates_selected", stages["topic"].id, {"artifact_ids": [topic.id]}),
            ("stage.confirmed", stages["topic"].id, {}),
            ("stage.candidates_selected", stages["copy"].id, {"artifact_ids": [old_copy.id]}),
            ("artifact.revised", copy.id, {"source_id": old_copy.id}),
            (
                "stage.candidates_selected",
                stages["image"].id,
                {"artifact_ids": [image3.id, image2.id]},
            ),
            ("stage.confirmed", stages["image"].id, {}),
            ("task.updated", task.id, {"version": 34}),
            ("task.updated", task.id, {"version": 35}),
        ]
        for offset, (action, resource_id, details) in enumerate(events, 1):
            _audit(session, task, action, resource_id, start + timedelta(seconds=offset), **details)
        session.flush()
        before = [(item.id, item.status) for item in (topic, old_copy, copy, image2, image3)]
        expected = [final.id, topic.id, copy.id, image3.id, image2.id]
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == expected
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == expected
        assert [
            (item.id, item.status) for item in (topic, old_copy, copy, image2, image3)
        ] == before
        assert all(stages[key].state == "stale" for key in ("topic", "copy", "image"))
        task.archived_at = None
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == [final.id]
        stages["export"].state = "confirmed"
        stages["export"].confirmed_at = start + timedelta(seconds=110)
        session.flush()
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == expected
        # A real edit invalidates completion, unlike merely unlocking the task.
        stages["export"].state = "stale"
        task.status = "draft"
        session.flush()
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == [final.id]


def test_configuration_preserves_actual_mapping_once_across_repeated_updates(content_client):
    from sqlalchemy import select

    from content_service.db import SessionLocal
    from content_service.export_selection import resolve_task_export_artifacts
    from content_service.models import AuditLog
    from content_service.services import dump
    from content_service.workflow import mark_configuration_changed

    with SessionLocal() as session:
        task, stages, final, add, _ = _setup(session)
        images = [add("image", 2), add("image", 3)]
        mapping = {"image": [item.id for item in images]}
        stages["image"].state = "confirmed"
        stages["image"].selected_artifact_ids_json = dump(mapping)
        stages["image"].confirmed_artifact_ids_json = dump(mapping)
        for item in images:
            item.status = "confirmed"
        task.archived_at = None
        mark_configuration_changed(session, task, task.owner_id)
        mark_configuration_changed(session, task, task.owner_id)
        session.flush()
        preserved = session.scalars(
            select(AuditLog).where(
                AuditLog.resource_id == stages["image"].id,
                AuditLog.action == "stage.selection_preserved",
            )
        ).all()
        assert len(preserved) == 1
        assert stages["image"].selected_artifact_ids_json == "{}"
        assert stages["image"].state == "stale"
        final.status = "confirmed"
        final.created_at = datetime.now(UTC) + timedelta(seconds=1)
        stages["video_preview"].state = "confirmed"
        stages["video_preview"].selected_artifact_ids_json = dump({"video": [final.id]})
        stages["video_preview"].confirmed_artifact_ids_json = dump({"video": [final.id]})
        task.archived_at = final.created_at + timedelta(seconds=1)
        session.flush()
        assert {item.id for item in resolve_task_export_artifacts(session, task)} == {
            final.id,
            *mapping["image"],
        }


def test_explicit_deselection_and_skip_do_not_revive_old_archived_choices(content_client):
    from content_service.db import SessionLocal
    from content_service.export_selection import resolve_task_export_artifacts

    with SessionLocal() as session:
        task, stages, final, add, start = _setup(session)
        topic, image = add("topic"), add("image")
        for offset, key, item in ((1, "topic", topic), (2, "image", image)):
            _audit(
                session,
                task,
                "stage.selection_preserved",
                stages[key].id,
                start + timedelta(seconds=offset),
                invalidated_by_stage="configuration",
                confirmed_artifact_ids={item.kind: [item.id]},
            )
        _audit(
            session,
            task,
            "stage.candidates_selected",
            stages["image"].id,
            start + timedelta(seconds=3),
            artifact_ids=[],
        )
        _audit(session, task, "stage.skipped", stages["topic"].id, start + timedelta(seconds=4))
        _audit(session, task, "task.updated", task.id, start + timedelta(seconds=5))
        session.flush()
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == [final.id]


def test_archive_snapshot_is_scoped_to_task_and_archive_and_filters_unusable_ids(content_client):
    from content_service.db import SessionLocal
    from content_service.export_selection import resolve_task_export_artifacts
    from content_service.models import ContentTask
    from content_service.services import dump

    with SessionLocal() as session:
        task, stages, final, add, _ = _setup(session)
        copy, discarded, future = add("copy", 2), add("image"), add("image", 2)
        discarded.status = "discarded"
        future.created_at = task.archived_at + timedelta(seconds=1)
        foreign_task = ContentTask(id=str(uuid4()), owner_id="user-b", mode="copy", name="其他用户")
        session.add(foreign_task)
        session.flush()
        foreign = add("copy", 3)
        foreign.task_id = foreign_task.id
        final.content_json = dump(
            {
                "deliverable_artifact_ids": [
                    copy.id,
                    discarded.id,
                    future.id,
                    foreign.id,
                    str(uuid4()),
                ],
                "deliverable_task_id": task.id,
                "deliverable_selection_archived_at": task.archived_at.isoformat(),
            }
        )
        # SQLite returns naive timestamps on subsequent requests.
        task.archived_at = task.archived_at.replace(tzinfo=None)
        session.flush()
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == [
            copy.id,
            final.id,
        ]
        archived_at = task.archived_at
        stages["export"].state = "confirmed"
        stages["export"].confirmed_at = archived_at
        final.content_json = dump(
            {
                "deliverable_artifact_ids": [copy.id, final.id],
                "deliverable_task_id": task.id,
                "deliverable_export_stage_version": stages["export"].version,
            }
        )
        task.archived_at = None
        session.flush()
        assert [item.id for item in resolve_task_export_artifacts(session, task)] == [
            copy.id,
            final.id,
        ]
        next_copy = add("copy", 4)
        next_copy.status = "confirmed"
        stages["copy"].state = "confirmed"
        stages["copy"].confirmed_artifact_ids_json = dump({"copy": [next_copy.id]})
        task.archived_at = archived_at + timedelta(seconds=10)
        stages["export"].version += 1
        session.flush()
        assert {item.id for item in resolve_task_export_artifacts(session, task)} == {
            next_copy.id,
            final.id,
        }
