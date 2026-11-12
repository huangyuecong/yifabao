import io
import json
from uuid import uuid4

from PIL import Image


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 18), "#17806f").save(buffer, "PNG")
    return buffer.getvalue()


def create_project(client, headers):
    task = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": "video",
            "name": f"工程素材-{uuid4().hex[:8]}",
            "topic": "工程素材 CRUD 测试",
        },
    )
    assert task.status_code == 200, task.text
    project = client.post(
        f"/api/v1/tasks/{task.json()['id']}/video-project",
        headers=headers,
    )
    assert project.status_code == 200, project.text
    return project.json()


def upload_image(client, headers, project_id, *, role="video", name="shot.png"):
    return client.post(
        f"/api/v1/video-projects/{project_id}/assets/upload",
        headers=headers,
        data={"role": role},
        files={"file": (name, png_bytes(), "image/png")},
    )


def test_project_asset_upload_read_download_patch_and_reference_protection(
    content_client,
    user_headers,
    other_headers,
):
    from content_service.db import SessionLocal
    from content_service.models import AssetRef, MediaDerivative, ProjectAsset
    from content_service.provider_dependencies import get_asset_store_port

    project = create_project(content_client, user_headers)
    uploaded = upload_image(content_client, user_headers, project["id"])
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()
    assert asset["role"] == "video"
    assert asset["active"] is True
    assert asset["original_url"].endswith(f"/{asset['id']}/download")
    assert "storage_key" not in json.dumps(asset)
    assert asset["metadata"]["probe_status"] in {"succeeded", "unavailable"}

    with SessionLocal() as session:
        stored_asset = session.get(ProjectAsset, asset["id"])
        assert stored_asset is not None
        asset_ref = session.get(AssetRef, stored_asset.asset_ref_id)
        assert asset_ref is not None
        assert asset_ref.entry_id
        assert asset_ref.version_id

    listed = content_client.get(
        f"/api/v1/video-projects/{project['id']}/assets",
        headers=user_headers,
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [asset["id"]]
    detail = content_client.get(
        f"/api/v1/video-projects/{project['id']}/assets/{asset['id']}",
        headers=user_headers,
    )
    assert detail.status_code == 200, detail.text
    assert content_client.get(asset["original_url"], headers=other_headers).status_code == 403
    original = content_client.get(asset["original_url"], headers=user_headers)
    assert original.status_code == 200, original.text
    assert original.content == png_bytes()
    assert original.headers["content-type"].startswith("image/png")
    streamed = content_client.get(
        f"/api/v1/video-projects/{project['id']}/assets/{asset['id']}/stream",
        headers={**user_headers, "Range": "bytes=1-9"},
    )
    assert streamed.status_code == 206, streamed.text
    assert streamed.content == png_bytes()[1:10]
    assert streamed.headers["accept-ranges"] == "bytes"
    assert streamed.headers["content-range"] == f"bytes 1-9/{len(png_bytes())}"

    # A derivative download resolves ownership through derivative -> project asset
    # -> project -> task and never exposes the object-storage key.
    asset_store = get_asset_store_port()
    derivative_key = f"video-projects/{project['id']}/test/{uuid4().hex}.jpg"
    derivative_store = asset_store.put_bytes(
        derivative_key,
        b"thumbnail-bytes",
        "image/jpeg",
    )
    with SessionLocal() as session:
        derivative = MediaDerivative(
            project_asset_id=asset["id"],
            kind="thumbnail",
            variant="test",
            status="ready",
            storage_key=derivative_store.key,
            media_type=derivative_store.media_type,
            checksum=derivative_store.checksum_sha256,
            metadata_json="{}",
        )
        session.add(derivative)
        session.commit()
        derivative_id = derivative.id
    assert (
        content_client.get(
            f"/api/v1/media-derivatives/{derivative_id}/download",
            headers=other_headers,
        ).status_code
        == 403
    )
    derivative_file = content_client.get(
        f"/api/v1/media-derivatives/{derivative_id}/download",
        headers=user_headers,
    )
    assert derivative_file.status_code == 200, derivative_file.text
    assert derivative_file.content == b"thumbnail-bytes"

    updated = content_client.patch(
        f"/api/v1/video-projects/{project['id']}/assets/{asset['id']}",
        headers=user_headers,
        json={"label": "品牌主画面", "role": "logo"},
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["label"], updated.json()["role"]) == (
        "品牌主画面",
        "logo",
    )
    assert (
        content_client.patch(
            f"/api/v1/video-projects/{project['id']}/assets/{asset['id']}",
            headers=other_headers,
            json={"label": "越权修改"},
        ).status_code
        == 403
    )

    current_project = content_client.get(
        f"/api/v1/video-projects/{project['id']}", headers=user_headers
    ).json()
    video_track = next(
        item
        for item in current_project["revision"]["timeline"]["tracks"]
        if item["kind"] == "video"
    )
    attached = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": current_project["current_revision"],
            "operations": [
                {
                    "op_id": "use-uploaded-asset",
                    "type": "add",
                    "track_id": video_track["id"],
                    "item": {
                        "id": "uploaded-shot",
                        "project_asset_id": asset["id"],
                        "duration_seconds": 3,
                    },
                }
            ],
        },
    )
    assert attached.status_code == 200, attached.text
    archive_url = f"/api/v1/video-projects/{project['id']}/assets/{asset['id']}"
    assert (
        content_client.patch(
            archive_url,
            headers=user_headers,
            json={"active": False},
        ).status_code
        == 409
    )
    assert content_client.delete(archive_url, headers=user_headers).status_code == 409

    removed = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": attached.json()["current_revision"],
            "operations": [
                {
                    "op_id": "release-uploaded-asset",
                    "type": "remove",
                    "item_id": "uploaded-shot",
                }
            ],
        },
    )
    assert removed.status_code == 200, removed.text
    archived = content_client.delete(archive_url, headers=user_headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["active"] is False
    # Archive hides the asset from the active library but keeps immutable
    # revision media downloadable and renderable for history/restore.
    assert content_client.get(asset["original_url"], headers=user_headers).status_code == 200
    assert (
        content_client.get(
            f"/api/v1/media-derivatives/{derivative_id}/download",
            headers=user_headers,
        ).status_code
        == 200
    )
    assert (
        content_client.get(
            f"/api/v1/video-projects/{project['id']}/assets?active_only=true",
            headers=user_headers,
        ).json()
        == []
    )

    restored = content_client.patch(
        archive_url,
        headers=user_headers,
        json={"active": True},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["active"] is True
    assert content_client.get(asset["original_url"], headers=user_headers).status_code == 200


def test_project_asset_upload_rejects_spoofed_media_and_incompatible_role(
    content_client,
    user_headers,
):
    project = create_project(content_client, user_headers)
    spoofed = content_client.post(
        f"/api/v1/video-projects/{project['id']}/assets/upload",
        headers=user_headers,
        data={"role": "video"},
        files={"file": ("fake.mp4", png_bytes(), "video/mp4")},
    )
    assert spoofed.status_code == 422
    assert "扩展名" in spoofed.text

    wrong_role = upload_image(
        content_client,
        user_headers,
        project["id"],
        role="music",
    )
    assert wrong_role.status_code == 422
    assert "不能用作" in wrong_role.text
    assert (
        content_client.get(
            f"/api/v1/video-projects/{project['id']}/assets",
            headers=user_headers,
        ).json()
        == []
    )


def test_project_asset_keeps_original_when_probe_tools_are_unavailable(
    content_client,
    user_headers,
    monkeypatch,
):
    import content_service.media_ingest as media_ingest

    project = create_project(content_client, user_headers)
    monkeypatch.setattr(media_ingest.shutil, "which", lambda _: None)
    monkeypatch.setattr(media_ingest, "_ffmpeg_executable", lambda *_: None)
    uploaded = upload_image(
        content_client,
        user_headers,
        project["id"],
        role="overlay",
        name="offline-probe.png",
    )
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()
    assert asset["metadata"]["probe_status"] == "unavailable"
    assert asset["metadata"]["derivative_status"] == "not_started"
    original = content_client.get(asset["original_url"], headers=user_headers)
    assert original.status_code == 200, original.text
    assert original.content == png_bytes()
