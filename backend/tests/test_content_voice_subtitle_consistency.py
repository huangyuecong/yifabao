import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from content_service.db import Base
from content_service.models import Artifact, ArtifactLink, ContentTask, GenerationRun
from content_service.workflow import (
    WorkflowConflictError,
    complete_generation_run,
    confirm_stage,
    get_stage,
    initialize_workflow,
    select_artifacts,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def _task_with_workflow(session: Session) -> ContentTask:
    task = ContentTask(owner_id="content-user", mode="full", name="配音字幕一致性测试")
    session.add(task)
    session.flush()
    initialize_workflow(session, task, "content-user")
    return task


def _artifact(
    session: Session,
    task: ContentTask,
    kind: str,
    *,
    source_artifact_id: str | None = None,
    status: str = "selected",
) -> Artifact:
    content = {"text": f"{kind} test content"}
    if source_artifact_id is not None:
        content["source_artifact_id"] = source_artifact_id
    artifact = Artifact(
        task_id=task.id,
        kind=kind,
        stage_key="voice_subtitles",
        status=status,
        content_json=json.dumps(content),
        created_by="content-user",
    )
    session.add(artifact)
    session.flush()
    return artifact


def _select_pair(stage, voice: Artifact, subtitle: Artifact) -> None:
    stage.selected_artifact_ids_json = json.dumps({"voice": [voice.id], "subtitle": [subtitle.id]})
    stage.confirmed_artifact_ids_json = stage.selected_artifact_ids_json
    stage.state = "confirmed"
    stage.confirmed_by = "content-user"


def test_generating_new_voice_unselects_but_preserves_previous_subtitle(db_session):
    task = _task_with_workflow(db_session)
    stage = get_stage(db_session, task, "voice_subtitles")
    previous_voice = _artifact(db_session, task, "voice")
    previous_subtitle = _artifact(
        db_session,
        task,
        "subtitle",
        source_artifact_id=previous_voice.id,
    )
    _select_pair(stage, previous_voice, previous_subtitle)

    run = GenerationRun(
        task_id=task.id,
        stage_key="voice_subtitles",
        status="running",
        requested_count=1,
        created_by="content-user",
    )
    new_voice = _artifact(db_session, task, "voice", status="draft")
    db_session.add(run)
    db_session.flush()

    complete_generation_run(db_session, task, run, [new_voice])

    assert json.loads(stage.selected_artifact_ids_json) == {"voice": [new_voice.id]}
    assert json.loads(stage.confirmed_artifact_ids_json) == {}
    assert previous_voice.status == "draft"
    assert previous_subtitle.status == "draft"
    assert stage.state == "awaiting_confirmation"


def test_selecting_new_voice_unselects_but_preserves_previous_subtitle(db_session):
    task = _task_with_workflow(db_session)
    stage = get_stage(db_session, task, "voice_subtitles")
    previous_voice = _artifact(db_session, task, "voice")
    previous_subtitle = _artifact(
        db_session,
        task,
        "subtitle",
        source_artifact_id=previous_voice.id,
    )
    new_voice = _artifact(db_session, task, "voice", status="draft")
    _select_pair(stage, previous_voice, previous_subtitle)

    select_artifacts(
        db_session,
        task,
        "voice_subtitles",
        [new_voice],
        expected_version=stage.version,
    )

    assert json.loads(stage.selected_artifact_ids_json) == {"voice": [new_voice.id]}
    assert previous_voice.status == "draft"
    assert previous_subtitle.status == "draft"
    assert stage.state == "awaiting_confirmation"


def test_switching_voice_preserves_subtitle_confirmed_by_linked_task(db_session):
    task = _task_with_workflow(db_session)
    stage = get_stage(db_session, task, "voice_subtitles")
    previous_voice = _artifact(db_session, task, "voice")
    previous_subtitle = _artifact(
        db_session,
        task,
        "subtitle",
        source_artifact_id=previous_voice.id,
    )
    new_voice = _artifact(db_session, task, "voice", status="draft")
    _select_pair(stage, previous_voice, previous_subtitle)

    linked_task = ContentTask(
        owner_id="content-user",
        mode="video",
        name="引用历史字幕的任务",
    )
    db_session.add(linked_task)
    db_session.flush()
    initialize_workflow(db_session, linked_task, "content-user")
    linked_stage = get_stage(db_session, linked_task, "voice_subtitles")
    linked_mapping = json.dumps({"subtitle": [previous_subtitle.id]})
    linked_stage.selected_artifact_ids_json = linked_mapping
    linked_stage.confirmed_artifact_ids_json = linked_mapping
    linked_stage.state = "confirmed"
    db_session.add(
        ArtifactLink(
            target_task_id=linked_task.id,
            source_task_id=task.id,
            artifact_id=previous_subtitle.id,
            stage_key="voice_subtitles",
            created_by="content-user",
        )
    )

    select_artifacts(
        db_session,
        task,
        "voice_subtitles",
        [new_voice],
        expected_version=stage.version,
    )

    assert json.loads(stage.selected_artifact_ids_json) == {"voice": [new_voice.id]}
    assert previous_voice.status == "draft"
    assert previous_subtitle.status == "confirmed"


def test_confirm_voice_subtitles_rejects_subtitle_from_another_voice(db_session):
    task = _task_with_workflow(db_session)
    stage = get_stage(db_session, task, "voice_subtitles")
    source_voice = _artifact(db_session, task, "voice")
    selected_voice = _artifact(db_session, task, "voice")
    mismatched_subtitle = _artifact(
        db_session,
        task,
        "subtitle",
        source_artifact_id=source_voice.id,
    )
    _select_pair(stage, selected_voice, mismatched_subtitle)
    stage.state = "awaiting_confirmation"

    with pytest.raises(WorkflowConflictError) as error:
        confirm_stage(
            db_session,
            task,
            "voice_subtitles",
            expected_version=stage.version,
            user_id="content-user",
        )

    assert "当前字幕并非由当前选中的配音生成" in str(error.value)
    assert stage.state == "awaiting_confirmation"


def test_confirm_voice_subtitles_accepts_matching_pair(db_session):
    task = _task_with_workflow(db_session)
    stage = get_stage(db_session, task, "voice_subtitles")
    voice = _artifact(db_session, task, "voice")
    subtitle = _artifact(
        db_session,
        task,
        "subtitle",
        source_artifact_id=voice.id,
    )
    _select_pair(stage, voice, subtitle)
    stage.state = "awaiting_confirmation"

    confirmed = confirm_stage(
        db_session,
        task,
        "voice_subtitles",
        expected_version=stage.version,
        user_id="content-user",
    )

    assert confirmed.state == "confirmed"
    assert voice.status == "confirmed"
    assert subtitle.status == "confirmed"
