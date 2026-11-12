import json
from uuid import uuid4


def test_replace_all_drops_subtitle_that_does_not_match_new_voice(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact, TaskStage

    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": f"配音替换回归-{uuid4().hex[:8]}",
            "topic": "臻品文化",
            "product": "臻品雅酿",
        },
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]

    with SessionLocal() as session:
        old_voice = Artifact(
            task_id=task_id,
            kind="voice",
            stage_key="voice_subtitles",
            version=1,
            status="confirmed",
            content_json=json.dumps({"text": "旧配音"}),
            created_by="user-a",
        )
        session.add(old_voice)
        session.flush()
        old_subtitle = Artifact(
            task_id=task_id,
            kind="subtitle",
            stage_key="voice_subtitles",
            version=1,
            status="confirmed",
            content_json=json.dumps({"source_artifact_id": old_voice.id}),
            created_by="user-a",
        )
        new_voice = Artifact(
            task_id=task_id,
            kind="voice",
            stage_key="voice_subtitles",
            version=2,
            status="draft",
            content_json=json.dumps({"text": "新配音"}),
            created_by="user-a",
        )
        session.add_all([old_subtitle, new_voice])
        session.flush()

        voice_stage = (
            session.query(TaskStage)
            .filter(
                TaskStage.task_id == task_id,
                TaskStage.stage_key == "voice_subtitles",
            )
            .one()
        )
        old_selection = json.dumps({"voice": [old_voice.id], "subtitle": [old_subtitle.id]})
        voice_stage.state = "confirmed"
        voice_stage.selected_artifact_ids_json = old_selection
        voice_stage.confirmed_artifact_ids_json = old_selection
        expected_version = voice_stage.version
        old_voice_id = old_voice.id
        old_subtitle_id = old_subtitle.id
        new_voice_id = new_voice.id
        session.commit()

    replaced = content_client.put(
        f"/api/v1/tasks/{task_id}/stages/voice_subtitles/selection",
        headers=user_headers,
        json={
            "expected_version": expected_version,
            "artifact_ids": [new_voice_id, old_subtitle_id],
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["selected_artifact_ids"] == {"voice": [new_voice_id]}

    with SessionLocal() as session:
        assert session.get(Artifact, old_voice_id).status == "draft"
        assert session.get(Artifact, old_subtitle_id).status == "draft"

    confirmation = content_client.post(
        f"/api/v1/tasks/{task_id}/stages/voice_subtitles/confirm",
        headers=user_headers,
        json={"expected_version": replaced.json()["version"]},
    )
    assert confirmation.status_code == 409, confirmation.text
    assert "相互匹配" in confirmation.json()["detail"]

    restored = content_client.put(
        f"/api/v1/tasks/{task_id}/stages/voice_subtitles/selection",
        headers=user_headers,
        json={
            "expected_version": replaced.json()["version"],
            "artifact_ids": [old_voice_id, old_subtitle_id],
        },
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["selected_artifact_ids"] == {
        "voice": [old_voice_id],
        "subtitle": [old_subtitle_id],
    }

    restored_confirmation = content_client.post(
        f"/api/v1/tasks/{task_id}/stages/voice_subtitles/confirm",
        headers=user_headers,
        json={"expected_version": restored.json()["version"]},
    )
    assert restored_confirmation.status_code == 200, restored_confirmation.text
    assert restored_confirmation.json()["state"] == "confirmed"

    with SessionLocal() as session:
        assert session.get(Artifact, old_voice_id).status == "confirmed"
        assert session.get(Artifact, old_subtitle_id).status == "confirmed"
        assert session.get(Artifact, new_voice_id).status == "draft"
