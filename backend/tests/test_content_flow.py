import io
import time
import zipfile

from docx import Document


def test_content_permissions_and_full_flow(content_client, user_headers, other_headers):
    rule = content_client.post(
        "/api/v1/sensitive-rules",
        headers=user_headers,
        json={"phrase": "绝对领先", "level": "forbidden"},
    )
    assert rule.status_code == 200, rule.text
    duplicate_rule = content_client.post(
        "/api/v1/sensitive-rules",
        headers=user_headers,
        json={"phrase": "绝对领先", "level": "forbidden"},
    )
    assert duplicate_rule.status_code == 200, duplicate_rule.text
    assert duplicate_rule.json()["id"] == rule.json()["id"]
    assert duplicate_rule.json()["created"] is False
    library = content_client.post(
        "/api/v1/library/items",
        headers=user_headers,
        json={
            "category": "brand",
            "name": "品牌表达规范",
            "text_content": "准确、克制、可核验",
            "data_level": "internal",
        },
    )
    assert library.status_code == 200, library.text
    created = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "full",
            "name": "品牌故事短视频",
            "topic": "清溪河谷与臻品文化",
            "product": "臻品雅酿",
            "audience": "成年消费者",
            "style": "故事型",
        },
    )
    assert created.status_code == 200, created.text
    task = created.json()
    assert (
        content_client.get(f"/api/v1/tasks/{task['id']}", headers=other_headers).status_code == 403
    )

    topics = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-topics", headers=user_headers, json={"count": 3}
    )
    assert len(topics.json()["content"]["candidates"]) == 3
    copy = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-copy", headers=user_headers, json={"count": 2}
    )
    assert copy.status_code == 200, copy.text
    revision = content_client.patch(
        f"/api/v1/artifacts/{copy.json()['id']}",
        headers=user_headers,
        json={
            "content": {"body": "这是绝对领先的产品"},
            "expected_version": copy.json()["version"],
        },
    )
    compliance = content_client.post(
        f"/api/v1/artifacts/{revision.json()['id']}/compliance-check", headers=user_headers
    )
    assert compliance.json()["passed"] is False
    assert compliance.json()["hits"][0]["phrase"] == "绝对领先"

    images = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-images", headers=user_headers, json={"count": 1}
    )
    assert images.status_code == 200, images.text
    assert images.json()[0]["storage_key"].endswith(".png")
    storyboard = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-storyboard", headers=user_headers, json={"count": 3}
    )
    assert len(storyboard.json()["content"]["shots"]) == 3
    voice = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-voice",
        headers=user_headers,
        json={"instruction": "臻品文化，源于长期坚持。"},
    )
    assert voice.status_code == 200, voice.text
    subtitles = content_client.post(
        f"/api/v1/tasks/{task['id']}/generate-subtitles",
        headers=user_headers,
        json={"instruction": "臻品文化，源于长期坚持。"},
    )
    assert subtitles.status_code == 200, subtitles.text

    started = content_client.post(f"/api/v1/tasks/{task['id']}/compose-video", headers=user_headers)
    assert started.status_code == 200, started.text
    job_id = started.json()["id"]
    for _ in range(120):
        job = content_client.get(f"/api/v1/jobs/{job_id}", headers=user_headers).json()
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.1)
    assert job["status"] == "succeeded", job

    artifacts = content_client.get(
        f"/api/v1/tasks/{task['id']}/artifacts", headers=user_headers
    ).json()
    video = next(item for item in artifacts if item["kind"] == "video")
    confirmed = content_client.post(
        f"/api/v1/artifacts/{video['id']}/confirm", headers=user_headers
    )
    assert confirmed.json()["status"] == "confirmed"
    video_file = content_client.get(
        f"/api/v1/artifacts/{video['id']}/download", headers=user_headers
    )
    assert video_file.status_code == 200
    assert len(video_file.content) > 1000

    workflow = content_client.get(
        f"/api/v1/tasks/{task['id']}/workflow", headers=user_headers
    ).json()
    export_stage = next(item for item in workflow["stages"] if item["key"] == "export")
    confirmed_export = content_client.post(
        f"/api/v1/tasks/{task['id']}/stages/export/confirm",
        headers=user_headers,
        json={"expected_version": export_stage["version"]},
    )
    assert confirmed_export.status_code == 200, confirmed_export.text

    archived_task = content_client.get(f"/api/v1/tasks/{task['id']}", headers=user_headers)
    assert archived_task.status_code == 200, archived_task.text
    assert archived_task.json()["archived"] is True
    assert (
        content_client.get(
            f"/api/v1/tasks/{task['id']}/export",
            headers=other_headers,
        ).status_code
        == 403
    )

    exported = content_client.get(
        f"/api/v1/tasks/{task['id']}/export",
        headers=user_headers,
    )
    assert exported.status_code == 200, exported.text

    confirmed_ids = {
        artifact_id
        for item in workflow["stages"]
        if item["applicable"] and item["state"] == "confirmed"
        for artifact_ids in item["confirmed_artifact_ids"].values()
        for artifact_id in artifact_ids
    }
    selected = {
        item["kind"]: item
        for item in artifacts
        if item["id"] in confirmed_ids and item["kind"] in {"topic", "copy", "image", "video"}
    }
    selected_video = selected["video"]
    expected_names = {
        "video": f"{task['name']}-成片-v{selected_video['version']}.mp4",
    }
    if topic := selected.get("topic"):
        expected_names["topic"] = f"{task['name']}-标题-v{topic['version']}.docx"
    if selected_copy := selected.get("copy"):
        expected_names["copy"] = f"{task['name']}-文案-v{selected_copy['version']}.docx"
    image_file = None
    if image := selected.get("image"):
        expected_names["image"] = f"{task['name']}-图片-v{image['version']}.png"
        image_file = content_client.get(
            f"/api/v1/artifacts/{image['id']}/download", headers=user_headers
        )
        assert image_file.status_code == 200, image_file.text

    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
        assert set(expected_names.values()) <= names
        assert all("/" not in name and "\\" not in name for name in names)
        assert not any(name.lower().endswith((".json", ".csv")) for name in names)
        assert not any(item["id"] in name for item in artifacts for name in names)
        if image_file is not None:
            assert archive.read(expected_names["image"]) == image_file.content
        assert archive.read(expected_names["video"]) == video_file.content

        if topic is not None:
            topic_document = Document(io.BytesIO(archive.read(expected_names["topic"])))
            topic_text = "\n".join(paragraph.text for paragraph in topic_document.paragraphs)
            topic_content = topic["content"]
            topic_candidates = topic_content.get("candidates", [])
            topic_index = topic_content.get("selected_candidate_index", 0)
            selected_topic = topic_candidates[topic_index]
            expected_title = (
                selected_topic.get("title") or selected_topic.get("text")
                if isinstance(selected_topic, dict)
                else str(selected_topic)
            )
            assert expected_title in topic_text

        if selected_copy is not None:
            copy_document = Document(io.BytesIO(archive.read(expected_names["copy"])))
            copy_text = "\n".join(paragraph.text for paragraph in copy_document.paragraphs)
            assert "这是绝对领先的产品" in copy_text
