import json
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]


def create_video_task(client, headers, *, mode="video"):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": mode,
            "name": f"视频工程-{uuid4().hex[:8]}",
            "topic": "视频工程回归测试",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_project(client, headers, task_id):
    response = client.post(
        f"/api/v1/tasks/{task_id}/video-project",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def track(project, kind):
    return next(item for item in project["revision"]["timeline"]["tracks"] if item["kind"] == kind)


def test_confirmed_workflow_images_sync_without_reactivating_archived_assets(
    content_client,
    user_headers,
):
    task = create_video_task(content_client, user_headers, mode="full")
    generated = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/image/generate",
        headers=user_headers,
        json={
            "count": 1,
            "parameters": {"prompt": "已确认产品图片"},
        },
    )
    assert generated.status_code == 200, generated.text
    image = generated.json()["artifacts"][0]
    confirmed = content_client.post(
        f"/api/v1/artifacts/{image['id']}/confirm",
        headers=user_headers,
    )
    assert confirmed.status_code == 200, confirmed.text

    project = create_project(content_client, user_headers, task["id"])
    synced = next(item for item in project["assets"] if item["artifact_id"] == image["id"])
    assert synced["active"] is True
    assert synced["role"] == "video"
    assert synced["metadata"]["source"] == "workflow-confirmed"
    assert synced["metadata"]["workflow_stage"] == "image"

    reassigned = content_client.patch(
        f"/api/v1/video-projects/{project['id']}/assets/{synced['id']}",
        headers=user_headers,
        json={"role": "overlay"},
    )
    assert reassigned.status_code == 200, reassigned.text
    repeated = create_project(content_client, user_headers, task["id"])
    role_preserved = next(item for item in repeated["assets"] if item["id"] == synced["id"])
    assert role_preserved["role"] == "overlay"

    archived = content_client.delete(
        f"/api/v1/video-projects/{project['id']}/assets/{synced['id']}",
        headers=user_headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["active"] is False
    repeated_after_archive = create_project(content_client, user_headers, task["id"])
    still_archived = next(
        item for item in repeated_after_archive["assets"] if item["id"] == synced["id"]
    )
    assert still_archived["active"] is False


def test_video_project_edit_operations_are_versioned_and_conflict_safe(
    content_client,
    user_headers,
    other_headers,
):
    task = create_video_task(content_client, user_headers)
    project = create_project(content_client, user_headers, task["id"])
    assert project["current_revision"] == 1
    assert project["revision"]["source"] == "initial"
    assert {item["kind"] for item in project["revision"]["timeline"]["tracks"]} == {
        "video",
        "overlay",
        "subtitle",
        "voice",
        "music",
        "sfx",
    }
    repeated = create_project(content_client, user_headers, task["id"])
    assert repeated["id"] == project["id"]
    assert (
        content_client.get(
            f"/api/v1/video-projects/{project['id']}", headers=other_headers
        ).status_code
        == 403
    )

    video_track_id = track(project, "video")["id"]
    added = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "operations": [
                {
                    "op_id": "add-title-card",
                    "type": "add",
                    "track_id": video_track_id,
                    "item": {
                        "id": "clip-a",
                        "label": "原始片段",
                        "content": "品牌片头占位画面",
                        "start_seconds": 0,
                        "duration_seconds": 8,
                    },
                }
            ],
        },
    )
    assert added.status_code == 200, added.text
    added_project = added.json()
    assert added_project["current_revision"] == 2
    restore_revision_id = added_project["current_revision_id"]
    assert track(added_project, "video")["items"][0]["id"] == "clip-a"

    stale = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "operations": [{"op_id": "stale-remove", "type": "remove", "item_id": "clip-a"}],
        },
    )
    assert stale.status_code == 409

    split = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 2,
            "operations": [
                {
                    "op_id": "split-clip",
                    "type": "split",
                    "item_id": "clip-a",
                    "at_seconds": 3,
                    "new_item_id": "clip-b",
                }
            ],
        },
    )
    assert split.status_code == 200, split.text
    split_project = split.json()
    split_items = track(split_project, "video")["items"]
    assert [(item["id"], item["duration_seconds"]) for item in split_items] == [
        ("clip-a", 3.0),
        ("clip-b", 5.0),
    ]

    trimmed = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 3,
            "operations": [
                {
                    "op_id": "trim-clip-b",
                    "type": "trim",
                    "item_id": "clip-b",
                    "trim_in_seconds": 3.5,
                    "trim_out_seconds": 0.5,
                    "duration_seconds": 4,
                }
            ],
        },
    )
    assert trimmed.status_code == 200, trimmed.text
    assert track(trimmed.json(), "video")["items"][1]["duration_seconds"] == 4

    moved = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 4,
            "operations": [
                {
                    "op_id": "move-clip-b",
                    "type": "move",
                    "item_id": "clip-b",
                    "track_id": video_track_id,
                    "index": 0,
                    "start_seconds": 0,
                }
            ],
        },
    )
    assert moved.status_code == 200, moved.text
    assert [item["id"] for item in track(moved.json(), "video")["items"]] == [
        "clip-b",
        "clip-a",
    ]

    updated = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 5,
            "operations": [
                {
                    "op_id": "update-clip-b",
                    "type": "update",
                    "item_id": "clip-b",
                    "changes": {"label": "已调整片段", "volume": 0.6},
                }
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    assert track(updated.json(), "video")["items"][0]["label"] == "已调整片段"

    removed = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 6,
            "operations": [{"op_id": "remove-clip-a", "type": "remove", "item_id": "clip-a"}],
        },
    )
    assert removed.status_code == 200, removed.text
    assert [item["id"] for item in track(removed.json(), "video")["items"]] == ["clip-b"]

    restored = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 7,
            "operations": [
                {
                    "op_id": "restore-first-edit",
                    "type": "restore",
                    "revision_id": restore_revision_id,
                }
            ],
        },
    )
    assert restored.status_code == 200, restored.text
    restored_project = restored.json()
    assert restored_project["current_revision"] == 8
    assert restored_project["revision"]["source"] == "restore"
    assert [item["id"] for item in track(restored_project, "video")["items"]] == ["clip-a"]
    revisions = content_client.get(
        f"/api/v1/video-projects/{project['id']}/revisions",
        headers=user_headers,
    )
    assert revisions.status_code == 200, revisions.text
    assert [item["revision"] for item in revisions.json()] == list(range(8, 0, -1))
    assert revisions.json()[0]["source"] == "restore"
    assert (
        content_client.get(
            f"/api/v1/video-projects/{project['id']}/revisions",
            headers=other_headers,
        ).status_code
        == 403
    )


def test_video_project_accepts_provisional_default_track_aliases(
    content_client,
    user_headers,
):
    task = create_video_task(content_client, user_headers)
    project = create_project(content_client, user_headers, task["id"])
    video_track_id = track(project, "video")["id"]
    music_track_id = track(project, "music")["id"]
    assert video_track_id != "track-video"
    assert music_track_id != "track-music"

    edited = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "operations": [
                {
                    "op_id": "add-from-new-editor-draft",
                    "type": "add",
                    "track_id": "track-video",
                    "track_kind": "video",
                    "item": {
                        "id": "new-editor-draft-item",
                        "content": "新建工程首次导入的占位画面",
                        "start_seconds": 0,
                        "duration_seconds": 4,
                    },
                },
                {
                    "op_id": "mute-from-new-editor-draft",
                    "type": "update_track",
                    "track_id": "track-music",
                    "changes": {"muted": True},
                },
            ],
        },
    )

    assert edited.status_code == 200, edited.text
    saved = edited.json()
    assert [item["id"] for item in track(saved, "video")["items"]] == ["new-editor-draft-item"]
    assert track(saved, "music")["muted"] is True
    assert [operation["track_id"] for operation in saved["revision"]["operations"]] == [
        video_track_id,
        music_track_id,
    ]


def test_canvas_track_operations_ai_adoption_and_timeline_duration_limit(
    content_client,
    user_headers,
):
    task = create_video_task(content_client, user_headers)
    project = create_project(content_client, user_headers, task["id"])
    video_track_id = track(project, "video")["id"]
    music_track_id = track(project, "music")["id"]

    configured = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "operations": [
                {
                    "op_id": "square-canvas",
                    "type": "update_canvas",
                    "changes": {"width": 1080, "height": 1080, "fps": 30},
                },
                {
                    "op_id": "lock-music",
                    "type": "update_track",
                    "track_id": music_track_id,
                    "changes": {"name": "品牌音乐", "muted": True, "locked": True},
                },
            ],
        },
    )
    assert configured.status_code == 200, configured.text
    configured_project = configured.json()
    assert configured_project["revision"]["timeline"]["canvas"] == {
        "width": 1080,
        "height": 1080,
        "fps": 30,
        "aspect_ratio": "1:1",
    }
    music = track(configured_project, "music")
    assert (music["name"], music["muted"], music["locked"]) == (
        "品牌音乐",
        True,
        True,
    )

    too_long = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 2,
            "operations": [
                {
                    "op_id": "over-limit",
                    "type": "add",
                    "track_id": video_track_id,
                    "item": {
                        "id": "over-limit-item",
                        "content": "超长占位",
                        "start_seconds": 590,
                        "duration_seconds": 11,
                    },
                }
            ],
        },
    )
    assert too_long.status_code == 422
    assert "600" in too_long.text

    valid_end = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 2,
            "operations": [
                {
                    "op_id": "at-limit",
                    "type": "add",
                    "track_id": video_track_id,
                    "item": {
                        "id": "at-limit-item",
                        "content": "十分钟结尾",
                        "start_seconds": 590,
                        "duration_seconds": 10,
                    },
                }
            ],
        },
    )
    assert valid_end.status_code == 200, valid_end.text
    assert valid_end.json()["current_revision"] == 3

    proposal = content_client.post(
        f"/api/v1/video-projects/{project['id']}/ai-edit-proposals",
        headers=user_headers,
        json={
            "expected_revision": 3,
            "prompt": "改为竖屏并恢复音乐轨",
            "operations": [
                {
                    "op_id": "vertical-canvas",
                    "type": "update_canvas",
                    "changes": {"width": 1080, "height": 1920},
                },
                {
                    "op_id": "unlock-music",
                    "type": "update_track",
                    "track_id": music_track_id,
                    "changes": {"muted": False, "locked": False},
                },
            ],
        },
    )
    assert proposal.status_code == 200, proposal.text
    comparison = content_client.get(
        f"/api/v1/ai-edit-proposals/{proposal.json()['id']}/compare",
        headers=user_headers,
    )
    assert comparison.status_code == 200, comparison.text
    candidate = {"revision": comparison.json()["candidate_revision"]}
    assert candidate["revision"]["timeline"]["canvas"]["aspect_ratio"] == "9:16"
    assert track(candidate, "music")["muted"] is False

    adopted = content_client.post(
        f"/api/v1/ai-edit-proposals/{proposal.json()['id']}/adopt",
        headers=user_headers,
        json={
            "expected_revision": 3,
            "selected_operation_ids": ["vertical-canvas", "unlock-music"],
        },
    )
    assert adopted.status_code == 200, adopted.text
    adopted_project = adopted.json()["project"]
    assert adopted_project["revision"]["timeline"]["canvas"]["aspect_ratio"] == "9:16"
    assert track(adopted_project, "music")["locked"] is False


def test_ai_edit_proposal_compare_partial_adopt_and_reject(
    content_client,
    user_headers,
    other_headers,
):
    uploaded = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "image",
            "category": "material",
            "name": f"工程图片-{uuid4().hex[:8]}",
            "data_level": "internal",
        },
        files={"file": ("shot.png", b"project-image", "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    task = create_video_task(content_client, user_headers)
    project = create_project(content_client, user_headers, task["id"])
    video_track_id = track(project, "video")["id"]
    attached = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "operations": [
                {
                    "op_id": "attach-image",
                    "type": "add",
                    "track_id": video_track_id,
                    "item": {
                        "id": "asset-clip",
                        "library_item_id": uploaded.json()["id"],
                        "label": "原素材",
                        "duration_seconds": 8,
                    },
                }
            ],
        },
    )
    assert attached.status_code == 200, attached.text
    project = attached.json()
    assert project["assets"][0]["library_item_id"] == uploaded.json()["id"]
    assert project["assets"][0]["metadata"] == {
        "attached_via": "video_project_api",
        "source": "enterprise-knowledge-base",
        "source_version": uploaded.json()["version"],
        "media_kind": "image",
        "media_type": "image/png",
        "resource_entry_id": uploaded.json()["resource_entry_id"],
    }
    assert track(project, "video")["items"][0]["project_asset_id"] == project["assets"][0]["id"]

    proposal_response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/ai-edit-proposals",
        headers=user_headers,
        json={
            "expected_revision": 2,
            "prompt": "片段名称更突出品牌并缩短开场",
            "operations": [
                {
                    "op_id": "rename",
                    "type": "update",
                    "item_id": "asset-clip",
                    "changes": {"label": "AI 品牌开场"},
                },
                {
                    "op_id": "shorten",
                    "type": "trim",
                    "item_id": "asset-clip",
                    "trim_in_seconds": 0,
                    "trim_out_seconds": 2,
                    "duration_seconds": 6,
                },
            ],
        },
    )
    assert proposal_response.status_code == 200, proposal_response.text
    proposal = proposal_response.json()
    assert proposal["status"] == "pending"

    current = content_client.get(
        f"/api/v1/video-projects/{project['id']}", headers=user_headers
    ).json()
    assert current["current_revision"] == 2
    assert track(current, "video")["items"][0]["label"] == "原素材"

    compare = content_client.get(
        f"/api/v1/ai-edit-proposals/{proposal['id']}/compare",
        headers=user_headers,
    )
    assert compare.status_code == 200, compare.text
    comparison = compare.json()
    assert comparison["base_revision"]["revision"] == 2
    assert comparison["candidate_revision"]["revision"] is None
    assert (
        track({"revision": comparison["candidate_revision"]}, "video")["items"][0]["label"]
        == "AI 品牌开场"
    )
    assert (
        content_client.get(
            f"/api/v1/ai-edit-proposals/{proposal['id']}/compare",
            headers=other_headers,
        ).status_code
        == 403
    )

    adopted = content_client.post(
        f"/api/v1/ai-edit-proposals/{proposal['id']}/adopt",
        headers=user_headers,
        json={"expected_revision": 2, "selected_operation_ids": ["rename"]},
    )
    assert adopted.status_code == 200, adopted.text
    adopted_body = adopted.json()
    assert adopted_body["proposal"]["status"] == "adopted"
    assert adopted_body["proposal"]["selected_operation_ids"] == ["rename"]
    adopted_item = track(adopted_body["project"], "video")["items"][0]
    assert adopted_item["label"] == "AI 品牌开场"
    assert adopted_item["duration_seconds"] == 8
    assert (
        content_client.post(
            f"/api/v1/ai-edit-proposals/{proposal['id']}/adopt",
            headers=user_headers,
            json={"expected_revision": 3, "selected_operation_ids": ["rename"]},
        ).status_code
        == 409
    )

    rejectable = content_client.post(
        f"/api/v1/video-projects/{project['id']}/ai-edit-proposals",
        headers=user_headers,
        json={
            "expected_revision": 3,
            "prompt": "尝试静音",
            "operations": [
                {
                    "op_id": "mute",
                    "type": "update",
                    "item_id": "asset-clip",
                    "changes": {"volume": 0},
                }
            ],
        },
    )
    assert rejectable.status_code == 200, rejectable.text
    rejected = content_client.post(
        f"/api/v1/ai-edit-proposals/{rejectable.json()['id']}/reject",
        headers=user_headers,
        json={"expected_revision": 3},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["proposal"]["status"] == "rejected"
    assert rejected.json()["project"]["current_revision"] == 3
    assert track(rejected.json()["project"], "video")["items"][0]["volume"] == 1


def test_ai_edit_proposal_can_generate_operations_without_client_side_changes(
    content_client,
    user_headers,
):
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": "AI 轻剪测试",
            "topic": "雅酿品牌故事",
            "audience": "成年品牌用户",
            "content_type": "短视频口播",
            "style": "品牌宣传型",
        },
    )
    assert task_response.status_code == 200, task_response.text
    project = create_project(content_client, user_headers, task_response.json()["id"])
    video_track_id = track(project, "video")["id"]
    edited = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "operations": [
                {
                    "op_id": "seed-ai-clip",
                    "type": "add",
                    "track_id": video_track_id,
                    "item": {
                        "id": "ai-clip",
                        "label": "开场",
                        "content": "品牌开场画面",
                        "duration_seconds": 10,
                    },
                }
            ],
        },
    )
    assert edited.status_code == 200, edited.text

    proposal_response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/ai-edit-proposals",
        headers=user_headers,
        json={
            "expected_revision": 2,
            "prompt": "改为竖屏并加快节奏",
            "operations": [],
            "target_item_ids": ["ai-clip"],
        },
    )
    assert proposal_response.status_code == 200, proposal_response.text
    proposal = proposal_response.json()
    assert {item["type"] for item in proposal["operations"]} == {
        "update_canvas",
        "update",
    }

    current = content_client.get(
        f"/api/v1/video-projects/{project['id']}", headers=user_headers
    ).json()
    assert current["current_revision"] == 2
    assert current["revision"]["timeline"]["canvas"]["aspect_ratio"] == "16:9"
    assert track(current, "video")["items"][0]["duration_seconds"] == 10

    compare = content_client.get(
        f"/api/v1/ai-edit-proposals/{proposal['id']}/compare",
        headers=user_headers,
    )
    assert compare.status_code == 200, compare.text
    candidate = {"revision": compare.json()["candidate_revision"]}
    assert candidate["revision"]["timeline"]["canvas"]["aspect_ratio"] == "9:16"
    assert track(candidate, "video")["items"][0]["duration_seconds"] == 8


def test_ai_edit_generation_requires_complete_task_brief(content_client, user_headers):
    task = create_video_task(content_client, user_headers)
    project = create_project(content_client, user_headers, task["id"])
    response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/ai-edit-proposals",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "prompt": "改为竖屏",
            "operations": [],
        },
    )
    assert response.status_code == 422
    assert "目标人群" in response.text


def test_video_project_ai_music_is_stored_as_project_asset(content_client, user_headers):
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": "AI 配乐测试",
            "topic": "品牌晚宴",
            "audience": "成年消费者",
            "content_type": "活动短片",
            "style": "沉稳克制",
        },
    )
    assert task_response.status_code == 200, task_response.text
    project = create_project(content_client, user_headers, task_response.json()["id"])

    response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-music",
        headers=user_headers,
        json={
            "expected_revision": 1,
            "prompt": "温暖、沉稳、无歌词的企业品牌配乐",
            "duration_seconds": 1,
            "audio_format": "wav",
            "label": "晚宴氛围配乐",
        },
    )
    assert response.status_code == 200, response.text
    asset = response.json()
    assert asset["role"] == "music"
    assert asset["label"] == "晚宴氛围配乐"
    assert asset["source_type"] == "artifact"
    assert asset["metadata"]["prompt"].startswith("温暖")
    assert asset["original_url"]

    downloaded = content_client.get(asset["original_url"], headers=user_headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content.startswith(b"RIFF")
    refreshed = content_client.get(
        f"/api/v1/video-projects/{project['id']}", headers=user_headers
    ).json()
    assert refreshed["current_revision"] == 1
    assert any(item["id"] == asset["id"] for item in refreshed["assets"])


def test_existing_composition_and_storyboard_are_imported_into_initial_revision(
    content_client,
    user_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import Artifact

    first_asset = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "image",
            "category": "material",
            "name": f"旧成片素材-{uuid4().hex[:8]}",
        },
        files={"file": ("legacy.png", b"legacy-image", "image/png")},
    )
    assert first_asset.status_code == 200, first_asset.text
    composition_task = create_video_task(content_client, user_headers)
    with SessionLocal() as session:
        session.add(
            Artifact(
                task_id=composition_task["id"],
                kind="video",
                version=1,
                status="draft",
                content_json=json.dumps(
                    {
                        "width": 1080,
                        "height": 1920,
                        "duration_seconds": 5,
                        "composition_spec": {
                            "width": 1080,
                            "height": 1920,
                            "fps": 30,
                            "clips": [
                                {
                                    "source_asset_id": first_asset.json()["id"],
                                    "duration_seconds": 5,
                                    "transition": "fade",
                                    "transition_duration_seconds": 0.4,
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                created_by="user-a",
            )
        )
        session.commit()
    imported = create_project(content_client, user_headers, composition_task["id"])
    assert imported["revision"]["source"] == "legacy_import"
    assert imported["revision"]["timeline"]["canvas"] == {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "aspect_ratio": "9:16",
    }
    assert track(imported, "video")["items"][0]["metadata"]["transition"] == "fade"
    assert imported["assets"][0]["library_item_id"] == first_asset.json()["id"]

    storyboard_task = create_video_task(content_client, user_headers)
    with SessionLocal() as session:
        session.add(
            Artifact(
                task_id=storyboard_task["id"],
                kind="storyboard",
                stage_key="storyboard",
                version=1,
                status="confirmed",
                content_json=json.dumps(
                    {
                        "shots": [
                            {
                                "sequence": 1,
                                "visual": "清溪河谷远景",
                                "subtitle": "山河酿造时光",
                                "duration": 4,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                created_by="user-a",
            )
        )
        session.commit()
    storyboard_project = create_project(content_client, user_headers, storyboard_task["id"])
    assert storyboard_project["revision"]["source"] == "legacy_import"
    assert track(storyboard_project, "video")["items"][0]["content"] == "清溪河谷远景"
    assert track(storyboard_project, "subtitle")["items"][0]["content"] == "山河酿造时光"


def test_content_0005_video_project_migration_upgrades_and_downgrades(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'content-video-projects.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_URL", database_url)
    config = Config(str(ROOT / "services" / "content_service" / "alembic.ini"))
    command.upgrade(config, "content_0004")
    engine = create_engine(database_url)
    before = set(inspect(engine).get_table_names())
    assert "video_projects" not in before

    command.upgrade(config, "content_0005")
    inspector = inspect(engine)
    assert {
        "video_projects",
        "video_project_revisions",
        "project_assets",
        "media_derivatives",
        "ai_edit_proposals",
    } <= set(inspector.get_table_names())
    assert {
        "task_id",
        "current_revision",
        "current_revision_id",
    } <= {column["name"] for column in inspector.get_columns("video_projects")}
    assert {
        "base_revision_id",
        "candidate_revision_id",
        "selected_operation_ids_json",
    } <= {column["name"] for column in inspector.get_columns("ai_edit_proposals")}

    command.downgrade(config, "content_0004")
    assert "video_projects" not in inspect(engine).get_table_names()
    command.downgrade(config, "base")
    engine.dispose()
