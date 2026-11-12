import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def create_task(client, headers):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": "full",
            "name": f"草稿作废-{uuid4().hex[:8]}",
            "topic": "清溪河谷与臻品文化",
            "product": "臻品雅酿",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def generate(client, headers, task_id, endpoint, **payload):
    response = client.post(
        f"/api/v1/tasks/{task_id}/{endpoint}",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


def discard(client, headers, artifact_id):
    return client.delete(f"/api/v1/artifacts/{artifact_id}", headers=headers)


def test_current_copy_cannot_be_deleted_and_returns_a_chinese_reason(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers)
    draft = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    copy_stage = next(item for item in workflow["stages"] if item["key"] == "copy")
    selected = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={
            "expected_version": copy_stage["version"],
            "artifact_ids": [draft["id"]],
        },
    )
    assert selected.status_code == 200, selected.text

    refused = discard(content_client, user_headers, draft["id"])
    assert refused.status_code == 409
    assert refused.json()["detail"] == "当前已确认的内容版本不能删除"
    assert "selected" not in refused.json()["detail"]

    current_workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    current_copy_stage = next(item for item in current_workflow["stages"] if item["key"] == "copy")
    cleared = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={
            "expected_version": current_copy_stage["version"],
            "artifact_ids": [],
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["selected_artifact_ids"] == {}

    artifacts = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()
    cleared_artifact = next(item for item in artifacts if item["id"] == draft["id"])
    assert cleared_artifact["status"] == "draft"

    removed = discard(content_client, user_headers, draft["id"])
    assert removed.status_code == 200, removed.text


def test_orphaned_selected_status_does_not_block_deletion(
    content_client,
    user_headers,
):
    from sqlalchemy import select

    from content_service.db import SessionLocal
    from content_service.models import Artifact, TaskStage

    task = create_task(content_client, user_headers)
    draft = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    with SessionLocal() as session:
        artifact = session.get(Artifact, draft["id"])
        assert artifact is not None
        artifact.status = "selected"
        copy_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task["id"],
                TaskStage.stage_key == "copy",
            )
        )
        assert copy_stage is not None
        copy_stage.selected_artifact_ids_json = "{}"
        copy_stage.confirmed_artifact_ids_json = "{}"
        session.commit()

    removed = discard(content_client, user_headers, draft["id"])
    assert removed.status_code == 200, removed.text
    assert removed.json()["status"] == "discarded"


def test_historical_confirmed_status_without_stage_reference_can_be_deleted(
    content_client,
    user_headers,
):
    from sqlalchemy import select

    from content_service.db import SessionLocal
    from content_service.models import Artifact, TaskStage

    task = create_task(content_client, user_headers)
    draft = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    with SessionLocal() as session:
        artifact = session.get(Artifact, draft["id"])
        assert artifact is not None
        artifact.status = "confirmed"
        copy_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task["id"],
                TaskStage.stage_key == "copy",
            )
        )
        assert copy_stage is not None
        copy_stage.selected_artifact_ids_json = "{}"
        copy_stage.confirmed_artifact_ids_json = "{}"
        session.commit()

    removed = discard(content_client, user_headers, draft["id"])
    assert removed.status_code == 200, removed.text
    assert removed.json()["status"] == "discarded"


def test_stage_confirmation_mapping_protects_artifact_even_if_status_drifted(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact

    task = create_task(content_client, user_headers)
    draft = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    confirmed = content_client.post(
        f"/api/v1/artifacts/{draft['id']}/confirm",
        headers=user_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    with SessionLocal() as session:
        artifact = session.get(Artifact, draft["id"])
        assert artifact is not None
        artifact.status = "draft"
        session.commit()

    refused = discard(content_client, user_headers, draft["id"])
    assert refused.status_code == 409
    assert refused.json()["detail"] == "当前已确认的内容版本不能删除"


def test_switching_current_candidate_demotes_old_version_and_allows_deletion(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers)
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/generate",
        headers=user_headers,
        json={"count": 2},
    )
    assert generated.status_code == 200, generated.text
    first_id, second_id = generated.json()["artifact_ids"]

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    copy_stage = next(item for item in workflow["stages"] if item["key"] == "copy")
    first_confirmed = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/copy/confirm",
        headers=user_headers,
        json={
            "expected_version": copy_stage["version"],
            "artifact_ids": [first_id],
        },
    )
    assert first_confirmed.status_code == 200, first_confirmed.text

    switched = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={
            "expected_version": first_confirmed.json()["version"],
            "artifact_ids": [second_id],
        },
    )
    assert switched.status_code == 200, switched.text
    after_switch = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()
    by_id = {item["id"]: item for item in after_switch}
    assert by_id[first_id]["status"] == "draft"
    assert by_id[second_id]["status"] == "confirmed"
    assert switched.json()["selected_artifact_ids"] == {"copy": [second_id]}
    assert switched.json()["confirmed_artifact_ids"] == {"copy": [second_id]}

    removed = discard(content_client, user_headers, first_id)
    assert removed.status_code == 200, removed.text
    refused = discard(content_client, user_headers, second_id)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "当前已确认的内容版本不能删除"


def test_saving_unchanged_generated_content_does_not_create_another_version(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers)
    generated = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    before = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()

    unchanged = content_client.patch(
        f"/api/v1/artifacts/{generated['id']}",
        headers=user_headers,
        json={
            "content": generated["content"],
            "expected_version": generated["version"],
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["id"] == generated["id"]

    after = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()
    assert len(after) == len(before)


def test_manual_edit_keeps_selected_candidate_snapshot_in_sync(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers)
    generated = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=2,
    )
    content = json.loads(json.dumps(generated["content"], ensure_ascii=False))
    assert len(content["candidates"]) == 2
    content["selected_candidate_index"] = 1
    content["selected_candidate"] = dict(content["candidates"][1])
    changed_body = f"{content['candidates'][1]['body']}\n人工修改已生效。"
    content["candidates"][1]["body"] = changed_body

    saved = content_client.patch(
        f"/api/v1/artifacts/{generated['id']}",
        headers=user_headers,
        json={
            "content": content,
            "expected_version": generated["version"],
        },
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["id"] != generated["id"]
    assert saved.json()["content"]["candidates"][1]["body"] == changed_body
    assert saved.json()["content"]["selected_candidate"]["body"] == changed_body


def test_ai_transform_preview_creates_version_only_after_user_saves(
    content_client,
    user_headers,
):
    task = create_task(content_client, user_headers)
    source = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    before = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()

    preview = content_client.post(
        f"/api/v1/artifacts/{source['id']}/transform-copy",
        headers=user_headers,
        json={
            "operation": "rewrite",
            "instruction": "调整表达但暂不保存",
            "preview": True,
        },
    )
    assert preview.status_code == 200, preview.text
    preview_content = preview.json()["content"]
    assert preview_content["candidates"]
    source_candidate = source["content"]["candidates"][0]
    preview_candidate = preview_content["candidates"][0]
    assert preview_candidate["title"] == source_candidate["title"]
    assert preview_candidate["body"] != source_candidate["body"]

    after_preview = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()
    assert len(after_preview) == len(before)

    saved = content_client.patch(
        f"/api/v1/artifacts/{source['id']}",
        headers=user_headers,
        json={
            "content": preview_content,
            "expected_version": source["version"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["id"] != source["id"]
    assert saved.json()["content"]["candidates"][0]["title"] == preview_candidate["title"]
    assert saved.json()["content"]["candidates"][0]["body"] == preview_candidate["body"]

    after_save = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    ).json()
    assert len(after_save) == len(before) + 1


def test_discard_draft_is_audited_idempotent_and_preserves_version_history(
    content_client,
    user_headers,
    other_headers,
):
    task = create_task(content_client, user_headers)
    draft = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow",
        headers=user_headers,
    ).json()
    copy_stage = next(item for item in workflow["stages"] if item["key"] == "copy")
    cleared = content_client.put(
        f"/api/v1/tasks/{task['id']}/stages/copy/selection",
        headers=user_headers,
        json={
            "expected_version": copy_stage["version"],
            "artifact_ids": [],
        },
    )
    assert cleared.status_code == 200, cleared.text

    denied = discard(content_client, other_headers, draft["id"])
    assert denied.status_code == 403

    removed = discard(content_client, user_headers, draft["id"])
    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "id": draft["id"],
        "status": "discarded",
        "discarded": True,
        "already_discarded": False,
    }

    repeated = discard(content_client, user_headers, draft["id"])
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["already_discarded"] is True

    listed = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts",
        headers=user_headers,
    )
    assert listed.status_code == 200
    assert draft["id"] not in {item["id"] for item in listed.json()}

    rejected_calls = [
        content_client.patch(
            f"/api/v1/artifacts/{draft['id']}",
            headers=user_headers,
            json={
                "content": {"body": "不能再编辑"},
                "expected_version": draft["version"],
            },
        ),
        content_client.post(
            f"/api/v1/artifacts/{draft['id']}/transform-copy",
            headers=user_headers,
            json={"operation": "rewrite"},
        ),
        content_client.post(
            f"/api/v1/artifacts/{draft['id']}/compliance-check",
            headers=user_headers,
        ),
        content_client.post(
            f"/api/v1/artifacts/{draft['id']}/confirm",
            headers=user_headers,
        ),
        content_client.get(
            f"/api/v1/artifacts/{draft['id']}/download",
            headers=user_headers,
        ),
        content_client.post(
            f"/api/v1/tasks/{task['id']}/generate-storyboard",
            headers=user_headers,
            json={"count": 1, "source_artifact_id": draft["id"]},
        ),
    ]
    assert {response.status_code for response in rejected_calls} == {409}

    successor = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    assert successor["version"] == draft["version"] + 1

    confirmed = content_client.post(
        f"/api/v1/artifacts/{successor['id']}/confirm",
        headers=user_headers,
    )
    assert confirmed.status_code == 200
    refused = discard(content_client, user_headers, successor["id"])
    assert refused.status_code == 409
    assert "已确认" in refused.json()["detail"]

    logs = content_client.get("/api/v1/audit-logs", headers=user_headers)
    matching = [
        row
        for row in logs.json()
        if row["action"] == "artifact.discarded" and row["resource_id"] == draft["id"]
    ]
    assert len(matching) == 1
    assert matching[0]["details"] == {
        "task_id": task["id"],
        "kind": "copy",
        "version": draft["version"],
        "previous_status": "draft",
    }


def test_discarded_artifacts_require_explicit_reselection_and_are_not_exported(
    content_client,
    user_headers,
    monkeypatch,
):
    from content_service import main as content_main

    task = create_task(content_client, user_headers)

    def select_stage(stage_key, artifact_ids):
        workflow = content_client.get(
            f"/api/v1/tasks/{task['id']}/workflow",
            headers=user_headers,
        ).json()
        current = next(item for item in workflow["stages"] if item["key"] == stage_key)
        response = content_client.put(
            f"/api/v1/tasks/{task['id']}/stages/{stage_key}/selection",
            headers=user_headers,
            json={
                "expected_version": current["version"],
                "artifact_ids": artifact_ids,
            },
        )
        assert response.status_code == 200, response.text

    active_copy = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    discarded_copy = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-copy",
        count=1,
    )
    select_stage("copy", [])
    assert discard(content_client, user_headers, discarded_copy["id"]).status_code == 200
    rejected_storyboard = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-storyboard",
        headers=user_headers,
        json={"count": 1},
    )
    assert rejected_storyboard.status_code == 409
    select_stage("copy", [active_copy["id"]])

    storyboard = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-storyboard",
        count=1,
    )
    assert storyboard["content"]["source_artifact_id"] == active_copy["id"]

    active_voice = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-voice",
        instruction="保留的配音文本",
    )
    discarded_voice = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-voice",
        instruction="已经作废的配音文本",
    )
    assert discard(content_client, user_headers, discarded_voice["id"]).status_code == 200
    rejected_subtitle = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-subtitles",
        headers=user_headers,
        json={"count": 1},
    )
    assert rejected_subtitle.status_code == 409
    select_stage("voice_subtitles", [active_voice["id"]])

    active_subtitle = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-subtitles",
    )
    assert active_subtitle["content"]["text"] == "保留的配音文本"
    discarded_subtitle = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-subtitles",
        instruction="已经作废的字幕文本",
    )
    assert discard(content_client, user_headers, discarded_subtitle["id"]).status_code == 200
    select_stage("voice_subtitles", [active_voice["id"], active_subtitle["id"]])

    active_image = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-images",
        count=1,
    )[0]
    discarded_image = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-images",
        count=1,
    )[0]
    assert discard(content_client, user_headers, discarded_image["id"]).status_code == 200
    select_stage("image", [active_image["id"]])
    select_stage("storyboard", [storyboard["id"]])
    select_stage("voice_subtitles", [active_voice["id"], active_subtitle["id"]])

    selected = {}

    def fake_start_video_job(
        task_id,
        owner_id,
        image_key,
        voice_key,
        subtitle_key,
        parameters,
    ):
        selected.update(
            {
                "task_id": task_id,
                "owner_id": owner_id,
                "image_key": image_key,
                "voice_key": voice_key,
                "subtitle_key": subtitle_key,
                "parameters": parameters,
            }
        )
        return SimpleNamespace(id="job-for-discard-test", kind="video_composition", status="queued")

    monkeypatch.setattr(content_main, "start_video_job", fake_start_video_job)
    composed = content_client.post(
        f"/api/v1/tasks/{task['id']}/compose-video",
        headers=user_headers,
        json={"parameters": {"quality": "preview"}},
    )
    assert composed.status_code == 200, composed.text
    assert selected == {
        "task_id": task["id"],
        "owner_id": "user-a",
        "image_key": active_image["storage_key"],
        "voice_key": active_voice["storage_key"],
        "subtitle_key": active_subtitle["storage_key"],
        "parameters": {"quality": "preview"},
    }

    exported = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert exported.status_code == 409
    assert exported.json()["detail"] == "请先确认导出阶段"


def test_export_rejects_task_with_only_discarded_artifacts(content_client, user_headers):
    task = create_task(content_client, user_headers)
    draft = generate(
        content_client,
        user_headers,
        task["id"],
        "generate-topics",
        count=1,
    )
    assert discard(content_client, user_headers, draft["id"]).status_code == 200

    response = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert response.status_code == 409
    assert "没有可导出内容" in response.json()["detail"]


def test_delete_artifact_openapi_contract(content_client, user_headers):
    runtime_contract = content_client.get("/openapi.json", headers=user_headers).json()
    operation = runtime_contract["paths"]["/api/v1/artifacts/{artifact_id}"]["delete"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/ArtifactDiscardResponse"}

    stored_contract = json.loads(
        (ROOT / "contracts" / "content.openapi.json").read_text(encoding="utf-8")
    )
    assert "delete" in stored_contract["paths"]["/api/v1/artifacts/{artifact_id}"]
