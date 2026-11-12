"""Card downloads render saved text, not serialized provider dictionaries."""

import json
from urllib.parse import unquote
from uuid import uuid4

import pytest


def _saved_artifact(client, headers, kind, content, *, stored=None):
    from content_service.main import SessionLocal
    from content_service.models import Artifact
    from content_service.services import storage

    created = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"mode": "full", "name": f"可读卡片下载-{uuid4().hex[:8]}"},
    )
    assert created.status_code == 200, created.text
    task = created.json()
    storage_key = None
    if stored is not None:
        storage_key = f"tasks/{task['id']}/old-download.{stored[0]}"
        storage.path_for(storage_key).write_bytes(stored[1])
    with SessionLocal() as session:
        artifact = Artifact(
            task_id=task["id"],
            kind=kind,
            stage_key=kind,
            version=3,
            status="draft",
            content_json=json.dumps(content, ensure_ascii=False),
            storage_key=storage_key,
            created_by=headers["X-User-Id"],
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id
    return task, artifact_id, storage_key


def _download(client, headers, artifact_id):
    response = client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=headers)
    assert response.status_code == 200, response.text
    return response


def _text(response):
    # Windows exports use CRLF; both conventions retain the same paragraphs.
    return response.content.decode("utf-8").replace("\r\n", "\n")


def _artifact_snapshot(artifact_id):
    from content_service.main import SessionLocal
    from content_service.models import Artifact

    with SessionLocal() as session:
        artifact = session.get(Artifact, artifact_id)
        return {
            column.name: getattr(artifact, column.name) for column in Artifact.__table__.columns
        }


@pytest.mark.parametrize("kind", ["topic", "copy"])
@pytest.mark.parametrize("extension", ["txt", "json"])
def test_card_download_renders_old_structured_content_without_mutating_source(
    content_client, user_headers, kind, extension
):
    from content_service.services import storage

    candidate = {
        "direction": "春节团圆",
        "title": "雅集在侧，年味更浓",
        "summary": "与重要的人共度时光。",
        "body": "第一段：回家团圆。\n\n第二段：举杯迎新春。",
        "source_ids": ["internal-source-uuid"],
    }
    content = {"candidates": [candidate], "provider": "internal-provider"}
    old_dump = f"任务：旧任务\n内容类型：{kind}\n版本：3\n1. {candidate}".encode()
    task, artifact_id, storage_key = _saved_artifact(
        content_client, user_headers, kind, content, stored=(extension, old_dump)
    )
    before = _artifact_snapshot(artifact_id)

    first = _download(content_client, user_headers, artifact_id)
    second = _download(content_client, user_headers, artifact_id)
    assert first.content == second.content
    text = _text(first)
    if kind == "topic":
        assert text == "雅集在侧，年味更浓\n\n创作方向：春节团圆\n"
    else:
        assert text == (
            "雅集在侧，年味更浓\n\n"
            "第一段：回家团圆。\n\n第二段：举杯迎新春。\n"
        )
    for internal in ("内容类型", "版本：", "任务：", "source_ids", "internal-", "{", "}"):
        assert internal not in text
    assert first.headers["content-type"].startswith("text/plain")
    label = "标题" if kind == "topic" else "文案"
    assert f"{task['name']}-{label}-v3.txt" in unquote(first.headers["content-disposition"])
    assert _artifact_snapshot(artifact_id) == before
    assert storage.path_for(storage_key).read_bytes() == old_dump


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"candidates": ["旧版标题"]}, "旧版标题\n"),
        ({"candidate": {"title": "单个标题"}}, "单个标题\n"),
        ({"title": "直接保存的标题"}, "直接保存的标题\n"),
        ({"text": "旧版文本字段"}, "旧版文本字段\n"),
        ({"selected_candidate": {"title": "旧版已选标题"}}, "旧版已选标题\n"),
        (
            {
                "candidates": [
                    {"title": "第一条", "summary": "第一条说明", "source_ids": ["secret"]},
                    "第二条",
                ],
                "selected_candidate_index": 1,
            },
            "1. 第一条\n\n2. 第二条\n",
        ),
    ],
)
def test_default_topic_download_supports_saved_envelopes_and_preserves_all_candidates(
    content_client, user_headers, content, expected
):
    _, artifact_id, _ = _saved_artifact(content_client, user_headers, "topic", content)
    response = _download(content_client, user_headers, artifact_id)
    assert _text(response) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"candidates": ["旧版文案正文"]}, "旧版文案正文\n"),
        ({"candidate": {"title": "标题", "body": "正文"}}, "标题\n\n正文\n"),
        ({"title": "标题", "summary": None, "body": "正文"}, "标题\n\n正文\n"),
        (
            {"candidates": [{"title": "甲", "body": "甲正文"}, {"title": "乙", "body": "乙正文"}]},
            "甲\n\n甲正文\n\n乙\n\n乙正文\n",
        ),
    ],
)
def test_default_copy_download_supports_saved_envelopes_and_paragraphs(
    content_client, user_headers, content, expected
):
    _, artifact_id, _ = _saved_artifact(content_client, user_headers, "copy", content)
    assert _text(_download(content_client, user_headers, artifact_id)) == expected


def test_readable_card_download_does_not_relax_explicit_title_export_confirmation(
    content_client, user_headers
):
    _, artifact_id, _ = _saved_artifact(
        content_client, user_headers, "topic", {"candidates": [{"title": "待选标题"}]}
    )
    assert _text(_download(content_client, user_headers, artifact_id)) == "待选标题\n"
    explicit = content_client.get(
        f"/api/v1/artifacts/{artifact_id}/download?format=txt", headers=user_headers
    )
    assert explicit.status_code == 409
    assert "当前已选择并确认" in explicit.json()["detail"]


def test_empty_topic_download_returns_readable_error_not_a_dictionary(content_client, user_headers):
    _, artifact_id, _ = _saved_artifact(
        content_client, user_headers, "topic", {"candidates": [{"source_ids": ["only-internal"]}]}
    )
    response = content_client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=user_headers)
    assert response.status_code == 422
    assert response.json()["detail"] == "当前标题没有可下载的文字内容"


@pytest.mark.parametrize(
    ("kind", "extension", "media_type"),
    [
        ("image", "png", "image/png"),
        ("video", "mp4", "video/mp4"),
        ("storyboard", "json", "application/json"),
    ],
)
def test_other_stored_artifact_downloads_still_return_original_file(
    content_client, user_headers, kind, extension, media_type
):
    original = b"unchanged existing media file"
    _, artifact_id, _ = _saved_artifact(
        content_client, user_headers, kind, {"ignored": "metadata"}, stored=(extension, original)
    )
    response = _download(content_client, user_headers, artifact_id)
    assert response.content == original
    assert response.headers["content-type"].startswith(media_type)


def test_unstored_storyboard_download_remains_json(content_client, user_headers):
    content = {"shots": [{"content": "分镜内容", "duration": 4}], "source_ids": ["reference"]}
    _, artifact_id, _ = _saved_artifact(content_client, user_headers, "storyboard", content)
    response = _download(content_client, user_headers, artifact_id)
    assert response.headers["content-type"].startswith("application/json")
    assert json.loads(response.content) == content
