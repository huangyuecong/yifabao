import io
from uuid import uuid4

from PIL import Image
from sqlalchemy import func, select
from test_content_task_references import CapturingImageProvider

from content_service.db import SessionLocal
from content_service.main import app
from content_service.models import LibraryItem, ResourceChunk, ResourceEntry
from content_service.provider_dependencies import get_image_generation_port


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(stream, "PNG")
    return stream.getvalue()


def create_task(client, headers):
    response = client.post(
        "/api/v1/tasks", headers=headers,
        json={"mode": "image", "name": f"reference-{uuid4().hex}", "topic": "产品场景"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_task_reference_is_previewable_and_used_for_generation_without_library_publication(
    content_client, user_headers, monkeypatch,
):
    task_id = create_task(content_client, user_headers)
    with SessionLocal() as session:
        before = session.scalar(select(func.count(LibraryItem.id)))
    content = image_bytes()
    response = content_client.post(
        f"/api/v1/tasks/{task_id}/image-references", headers=user_headers,
        files={"file": ("reference.png", content, "image/png")},
    )
    assert response.status_code == 200, response.text
    reference = response.json()
    assert reference["source_type"] == "task-image-reference"
    assert reference["metadata"]["task_id"] == task_id
    preview = content_client.get(reference["download_url"], headers=user_headers)
    assert preview.status_code == 200
    assert preview.content == content

    provider = CapturingImageProvider()
    monkeypatch.setitem(app.dependency_overrides, get_image_generation_port, lambda: provider)
    generated = content_client.post(
        f"/api/v1/tasks/{task_id}/generate-images", headers=user_headers,
        json={"count": 1, "parameters": {"reference_asset_ids": [reference["id"]]}},
    )
    assert generated.status_code == 200, generated.text
    assert provider.last_request.reference_images == (content,)
    assert provider.last_request.reference_media_types == ("image/png",)
    with SessionLocal() as session:
        assert session.scalar(select(func.count(LibraryItem.id))) == before
        assert session.get(LibraryItem, reference["id"]) is None
        assert session.scalar(select(func.count(ResourceChunk.id)).where(
            ResourceChunk.version_id == reference["resource_version_id"],
        )) == 0
    listed = content_client.get("/api/v1/library/items", headers=user_headers).json()
    assert reference["id"] not in {item["resource_entry_id"] for item in listed}


def test_reference_upload_and_preview_enforce_task_access(content_client, user_headers, other_headers):
    task_id = create_task(content_client, user_headers)
    url = f"/api/v1/tasks/{task_id}/image-references"
    files = {"file": ("reference.png", image_bytes(), "image/png")}
    assert content_client.post(url, headers=other_headers, files=files).status_code == 403
    response = content_client.post(url, headers=user_headers, files=files)
    assert response.status_code == 200, response.text
    reference = response.json()
    assert content_client.get(reference["download_url"], headers=other_headers).status_code == 403
    other_task = create_task(content_client, user_headers)
    wrong_url = f"/api/v1/tasks/{other_task}/image-references/{reference['id']}/download"
    assert content_client.get(wrong_url, headers=user_headers).status_code == 404
    cross_task = content_client.post(
        f"/api/v1/tasks/{other_task}/generate-images", headers=user_headers,
        json={"count": 1, "parameters": {"reference_asset_ids": [reference["id"]]}},
    )
    assert cross_task.status_code == 422, cross_task.text
    assert "不属于当前任务" in cross_task.json()["detail"]


def test_reference_upload_rejects_non_images_before_creating_resources(content_client, user_headers):
    task_id = create_task(content_client, user_headers)
    with SessionLocal() as session:
        before = session.scalar(select(func.count(ResourceEntry.id)))
    for filename, data, mime in [
        ("notes.txt", b"hello", "text/plain"),
        ("fake.png", b"not an image", "image/png"),
        ("empty.png", b"", "image/png"),
    ]:
        response = content_client.post(
            f"/api/v1/tasks/{task_id}/image-references", headers=user_headers,
            files={"file": (filename, data, mime)},
        )
        assert response.status_code == 422, response.text
    with SessionLocal() as session:
        assert session.scalar(select(func.count(ResourceEntry.id))) == before
