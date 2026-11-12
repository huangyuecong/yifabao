"""File uploads must remain media even when the form started as a document."""

import base64
import subprocess

import imageio_ffmpeg
import pytest

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG2kAAAAASUVORK5CYII="
)


@pytest.fixture(scope="module")
def media_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp("library-media")
    result = {}
    for filename, source in [
        ("clip.mp4", "color=c=blue:s=64x64:r=10:d=0.3"),
        ("voice.wav", "sine=frequency=440:duration=0.3"),
        ("voice.mp3", "sine=frequency=440:duration=0.3"),
    ]:
        output = directory / filename
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                source,
                "-y",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        result[filename] = output.read_bytes()
    return result


@pytest.mark.parametrize(
    ("requested", "filename", "declared_type", "expected", "stored_type", "body"),
    [
        ("product", "product.png", "image/png", "product", "image/png", PNG),
        ("logo", "brand.png", "image/png", "logo", "image/png", PNG),
        ("document", "clip.mp4", "video/mp4", "video", "video/mp4", None),
        ("spec", "voice.wav", "audio/wav", "audio", "audio/wav", None),
        (None, "image.PNG", "application/octet-stream", "image", "image/png", PNG),
        (None, "voice.mp3", "application/octet-stream", "audio", "audio/mpeg", None),
        ("product", "manual.txt", "text/plain", "product", "text/plain", "产品说明".encode()),
    ],
)
def test_library_upload_round_trip(
    content_client,
    user_headers,
    media_files,
    requested,
    filename,
    declared_type,
    expected,
    stored_type,
    body,
):
    body = media_files[filename] if body is None else body
    params = {"category": "上传回归", "name": filename}
    if requested:
        params["resource_kind"] = requested
    response = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params=params,
        files={"file": (filename, body, declared_type)},
    )
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["resource_kind"] == expected
    assert item["has_asset"] is True
    assert item["category"] == "上传回归"
    listing = content_client.get("/api/v1/library/items", headers=user_headers).json()
    assert any(row["id"] == item["id"] for row in listing)
    download = content_client.get(item["download_url"], headers=user_headers)
    assert download.status_code == 200
    assert download.content == body
    assert download.headers["content-type"].startswith(stored_type)
    if expected in {"audio", "video"}:
        assert item["metadata"]["duration_seconds"] > 0
    if expected == "product" and filename.endswith(".txt"):
        assert "产品说明" in item["text_content"]


def test_library_upload_rejects_explicit_media_mismatch_before_creating_item(
    content_client, user_headers
):
    before = content_client.get("/api/v1/library/items", headers=user_headers).json()
    response = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={"resource_kind": "video", "category": "上传回归", "name": "错选类型"},
        files={"file": ("image.png", PNG, "image/png")},
    )
    assert response.status_code == 422
    assert "资源类型与文件不一致" in response.json()["detail"]
    after = content_client.get("/api/v1/library/items", headers=user_headers).json()
    assert {item["id"] for item in after} == {item["id"] for item in before}


def test_library_replacement_infers_media_instead_of_running_document_parser(
    content_client, user_headers
):
    initial = content_client.post(
        "/api/v1/library/items",
        headers=user_headers,
        json={
            "name": "企业资料",
            "category": "上传回归",
            "resource_kind": "document",
            "text_content": "旧资料正文",
        },
    ).json()
    response = content_client.post(
        f"/api/v1/library/items/{initial['id']}/versions/upload",
        headers=user_headers,
        params={"expected_version": initial["version"], "category": "上传回归", "name": "企业图片"},
        files={"file": ("new.png", PNG, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["resource_kind"] == "image"
    assert item["version"] == initial["version"] + 1
    download = content_client.get(item["download_url"], headers=user_headers)
    assert download.content == PNG


@pytest.mark.parametrize(
    "filename,body,mime",
    [("photo.png", PNG, "image/png"), ("manual.txt", b"extracted file text", "text/plain")],
)
def test_upload_and_replacement_keep_authored_text_and_attachment(
    content_client, user_headers, filename, body, mime
):
    text = "用户填写的资料正文，与文件中的内容不同。"
    upload = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={"category": "文本与附件", "name": filename},
        data={"text_content": text},
        files={"file": (filename, body, mime)},
    )
    assert upload.status_code == 200, upload.text
    first = upload.json()
    assert first["text_content"] == text
    assert content_client.get(first["download_url"], headers=user_headers).content == body
    replacement = content_client.post(
        f"/api/v1/library/items/{first['id']}/versions/upload",
        headers=user_headers,
        params={"category": "文本与附件", "name": filename, "expected_version": first["version"]},
        data={"text_content": text + "新版补充。"},
        files={"file": (filename, body, mime)},
    )
    assert replacement.status_code == 200, replacement.text
    second = replacement.json()
    assert second["text_content"] == text + "新版补充。"
    assert content_client.get(second["download_url"], headers=user_headers).content == body
    # Cancelling a subsequent replacement saves text only, retaining the attachment.
    edited = content_client.post(
        f"/api/v1/library/items/{second['id']}/versions",
        headers=user_headers,
        json={
            "category": second["category"],
            "name": second["name"],
            "resource_kind": second["resource_kind"],
            "text_content": "仅编辑文本",
            "expected_version": second["version"],
            "metadata": second["metadata"],
        },
    )
    assert edited.status_code == 200, edited.text
    third = edited.json()
    assert third["has_asset"] is True
    assert third["storage_key"] == second["storage_key"]
    assert third["text_content"] == "仅编辑文本"
    download = content_client.get(third["download_url"], headers=user_headers)
    assert download.status_code == 200, download.text
    assert download.content == body
    detail = content_client.get(f"/api/v1/library/items/{third['id']}", headers=user_headers).json()
    assert detail["text_content"] == "仅编辑文本"
    assert detail["assets"]
    from sqlalchemy import select

    from content_service.db import SessionLocal
    from content_service.models import ResourceChunk

    with SessionLocal() as session:
        chunks = session.scalars(
            select(ResourceChunk).where(ResourceChunk.version_id == third["resource_version_id"])
        ).all()
        assert "".join(chunk.text for chunk in chunks) == "仅编辑文本"


def test_upload_rejects_explicit_blank_description_before_write(content_client, user_headers):
    before = content_client.get("/api/v1/library/items", headers=user_headers).json()
    response = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={"category": "文本与附件", "name": "空说明"},
        data={"text_content": "   "},
        files={"file": ("photo.png", PNG, "image/png")},
    )
    assert response.status_code == 422
    after = content_client.get("/api/v1/library/items", headers=user_headers).json()
    assert {item["id"] for item in before} == {item["id"] for item in after}
