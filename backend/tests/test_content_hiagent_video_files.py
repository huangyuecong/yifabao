import base64
import json
from pathlib import Path

import pytest

from content_service import hiagent_quickstart
from content_service.hiagent_adapters import (
    HiAgentVideoGenerationPort,
    _hiagent_video_task_id,
    prepare_video_file_input,
)
from content_service.providers import AiVideoGenerationRequest

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _FakeResponse:
    def __init__(self, *, status_code=500, content=b"", headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        if payload is not None and not content:
            self.content = json.dumps(payload).encode("utf-8")
        else:
            self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("empty response")
        return self._payload


class _FakeHiAgentClient:
    workspace_id = "ws-test"

    def __init__(self):
        self.uploaded = []
        self.calls = []

    def list_granted_models(self):
        return [{"ID": "pro-model", "Granted": True}]

    def upload_raw(self, content, *, content_type, filename):
        self.uploaded.append(
            {"filename": filename, "content_type": content_type, "size": len(content)}
        )
        return {
            "Path": f"upload/full/{filename}",
            "Size": len(content),
            "Url": (
                "https://hiagent.example.com/api/proxy/down?Action=DownloadForPresign"
                f"&Key=test&Path=upload/full/{filename}"
            ),
        }

    def _request(self, action, body, timeout=None):
        self.calls.append((action, body))
        if action == "CreateVideoGenTask":
            return {"task_id": "task-wanted"}
        if action == "GetVideoGenTask":
            return {
                "Tasks": [
                    {
                        "TaskID": "task-other",
                        "Status": "failed",
                        "Data": [{"Url": "https://x/old.mp4"}],
                    },
                    {
                        "TaskID": "task-wanted",
                        "Status": "succeeded",
                        "Model": "pro-model",
                        "Data": [{"Url": "https://x/new.mp4"}],
                    },
                ]
            }
        return {}


def test_create_video_empty_gateway_error_is_generic(monkeypatch):
    monkeypatch.setattr(
        hiagent_quickstart.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(),
    )
    with pytest.raises(RuntimeError, match="视频服务返回了空错误"):
        hiagent_quickstart.call(
            "CreateVideoGenTask",
            {"Prompt": "test", "Files": [{"Url": "https://example.com/a.mp4"}]},
            timeout=5,
        )


def test_create_image_edit_empty_gateway_error_is_not_video(monkeypatch):
    monkeypatch.setattr(
        hiagent_quickstart.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(),
    )
    with pytest.raises(RuntimeError, match="图片服务返回了空错误") as failed:
        hiagent_quickstart.call(
            "CreateImageEdit",
            {"Prompt": "改图", "ImageFiles": [{"Url": "https://example.com/a.png"}]},
            timeout=5,
        )
    assert "视频" not in str(failed.value)


def test_create_video_parses_json_without_json_content_type(monkeypatch):
    monkeypatch.setattr(
        hiagent_quickstart.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            payload={"Result": {"task_id": "task-from-plain"}},
        ),
    )
    result = hiagent_quickstart.call("CreateVideoGenTask", {"Prompt": "参考视频"}, timeout=5)
    assert result == {"task_id": "task-from-plain"}


def test_create_video_parses_string_result_without_json_content_type(monkeypatch):
    monkeypatch.setattr(
        hiagent_quickstart.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/plain"},
            payload={"Result": "task-string-id"},
        ),
    )
    result = hiagent_quickstart.call("CreateVideoGenTask", {"Prompt": "参考视频"}, timeout=5)
    assert result == "task-string-id"
    assert _hiagent_video_task_id(result) == "task-string-id"


def test_prepare_video_file_input_uploads_data_url_and_nests_path():
    uploaded = {}

    def uploader(content, *, content_type, filename):
        uploaded["content"] = content
        uploaded["content_type"] = content_type
        uploaded["filename"] = filename
        return {
            "Path": "upload/full/aa/bb/probe",
            "Size": len(content),
            "Url": "https://hiagent.example.com/api/proxy/down?Action=DownloadForPresign&Key=k&Path=upload/full/aa/bb/probe",
        }

    url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
    result = prepare_video_file_input(
        url,
        name="reference_image_1.png",
        kind="image",
        role="reference_image",
        uploader=uploader,
    )
    assert result["Role"] == "reference_image"
    assert result["File"]["Path"] == "upload/full/aa/bb/probe"
    assert result["File"]["Type"] == "image"
    assert result["File"]["ContentType"] == "image/png"
    assert result["File"]["Url"].startswith("https://hiagent.example.com/api/proxy/down?")
    assert len(uploaded["content"]) > len(TINY_PNG)


def test_prepare_video_file_input_reuses_existing_upload_path():
    url = (
        "https://hiagent.example.com/api/proxy/down?Action=DownloadForPresign"
        "&Key=abc&Path=upload%2Ffull%2F71%2F94%2Fexisting"
    )
    result = prepare_video_file_input(
        url,
        name="clip.mp4",
        kind="video",
        role="reference_video",
        uploader=lambda *args, **kwargs: pytest.fail("should not re-upload"),
    )
    assert result == {
        "File": {
            "Path": "upload/full/71/94/existing",
            "Name": "clip.mp4",
            "Type": "video",
            "ContentType": "video/mp4",
            "Url": url,
        },
        "Role": "reference_video",
    }


def test_prepare_video_file_input_absolutizes_relative_download_url():
    url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
    result = prepare_video_file_input(
        url,
        name="x.png",
        kind="image",
        role="first_frame",
        uploader=lambda content, *, content_type, filename: {
            "Path": "upload/full/x",
            "Size": len(content),
            "Url": "/api/proxy/down?Action=DownloadForPresign&Key=abc==&Path=upload%2Ffull%2Fx",
        },
    )
    assert result["File"]["Url"] == (
        "https://hiagent.example.com/api/proxy/down"
        "?Action=DownloadForPresign&Key=abc==&Path=upload%2Ffull%2Fx"
    )


def test_prepare_video_file_input_reads_local_file(tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake-mp4-bytes")
    captured = {}

    def uploader(content, *, content_type, filename):
        captured.update(content=content, content_type=content_type, filename=filename)
        return {
            "Path": "upload/full/video",
            "Size": len(content),
            "Url": "https://hiagent.example.com/api/proxy/down?Action=DownloadForPresign&Key=k&Path=upload/full/video",
        }

    result = prepare_video_file_input(
        source.resolve().as_uri(),
        name="reference_video_1.mp4",
        kind="video",
        role="reference_video",
        uploader=uploader,
    )
    assert captured["content"] == b"fake-mp4-bytes"
    assert captured["content_type"] == "video/mp4"
    assert result["File"]["Path"] == "upload/full/video"
    assert result["File"]["Url"].startswith("https://")
    assert result["Role"] == "reference_video"


def test_prepare_video_file_input_passes_through_public_https():
    result = prepare_video_file_input(
        "https://ark-output.example/clip.mp4?signature=temporary",
        name="clip.mp4",
        kind="video",
        role="reference_video",
        uploader=lambda *_args, **_kwargs: pytest.fail("should not re-upload public https"),
    )
    assert result == {
        "File": {
            "Name": "clip.mp4",
            "Type": "video",
            "ContentType": "video/mp4",
            "Url": "https://ark-output.example/clip.mp4?signature=temporary",
        },
        "Role": "reference_video",
    }


def test_prepare_video_file_input_compresses_large_local_video(monkeypatch, tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0" * 1_200_000)
    monkeypatch.setattr(
        "content_service.hiagent_adapters._compress_reference_video",
        lambda _content: b"small-mp4",
    )
    captured = {}

    def uploader(content, *, content_type, filename):
        captured.update(content=content, content_type=content_type, filename=filename)
        return {
            "Path": "upload/full/video",
            "Size": len(content),
            "Url": "https://hiagent.example.com/api/proxy/down?Action=DownloadForPresign&Key=k&Path=upload/full/video",
        }

    result = prepare_video_file_input(
        source.resolve().as_uri(),
        name="reference_video_1.mp4",
        kind="video",
        role="reference_video",
        uploader=uploader,
    )
    assert captured["content"] == b"small-mp4"
    assert captured["filename"] == "clip.mp4"
    assert result["File"]["Path"] == "upload/full/video"


def test_prepare_video_file_input_retries_upload_after_413(monkeypatch, tmp_path: Path):
    from content_service.llm_adapters import ProviderCallError

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"mid-size-mp4")
    monkeypatch.setattr(
        "content_service.hiagent_adapters._compress_reference_video",
        lambda _content: b"small-mp4",
    )
    attempts = []

    def uploader(content, *, content_type, filename):
        attempts.append(content)
        if content != b"small-mp4":
            raise ProviderCallError("文件上传服务返回了无法解析的错误（HTTP 413）")
        return {
            "Path": "upload/full/video",
            "Size": len(content),
            "Url": "https://hiagent.example.com/api/proxy/down?Action=DownloadForPresign&Key=k&Path=upload/full/video",
        }

    result = prepare_video_file_input(
        source.resolve().as_uri(),
        name="reference_video_1.mp4",
        kind="video",
        role="reference_video",
        uploader=uploader,
    )
    assert attempts == [b"mid-size-mp4", b"small-mp4"]
    assert result["File"]["Path"] == "upload/full/video"


def test_hiagent_video_submit_uploads_multimodal_references():
    client = _FakeHiAgentClient()
    port = HiAgentVideoGenerationPort(
        client=client,
        pro_model="pro-model",
        fast_model="fast-model",
    )
    image_url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
    task = port.submit(
        AiVideoGenerationRequest(
            prompt="结合这两个生成一个视频",
            image_url=image_url,
            reference_video_urls=(
                "https://hiagent.example.com/api/proxy/down?Path=upload%2Ffull%2Fvid",
            ),
            duration_seconds=5,
            ratio="adaptive",
            resolution="480p",
        )
    )
    assert task.provider_task_id == "task-wanted"
    action, payload = client.calls[0]
    assert action == "CreateVideoGenTask"
    files = payload["Files"]
    assert files[0]["Role"] == "reference_image"
    assert files[0]["File"]["Path"].startswith("upload/full/")
    assert files[0]["File"]["ContentType"] == "image/png"
    assert files[0]["File"]["Url"].startswith("https://hiagent.example.com/")
    assert files[1]["Role"] == "reference_video"
    assert files[1]["File"]["Path"] == "upload/full/vid"
    assert files[1]["File"]["ContentType"] == "video/mp4"
    assert client.uploaded[0]["filename"] == "reference_image_1.png"


def test_hiagent_video_submit_image_only_and_video_only_keep_prompt():
    client = _FakeHiAgentClient()
    port = HiAgentVideoGenerationPort(
        client=client,
        pro_model="pro-model",
        fast_model="fast-model",
    )
    image_url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
    port.submit(
        AiVideoGenerationRequest(
            prompt="根据这张图生成视频",
            image_url=image_url,
            duration_seconds=5,
            ratio="adaptive",
            resolution="480p",
        )
    )
    image_payload = client.calls[0][1]
    assert image_payload["Prompt"] == "根据这张图生成视频"
    assert image_payload["Files"][0]["Role"] == "first_frame"
    assert not image_payload["Files"][0]["File"]["Url"].startswith("data:")

    port.submit(
        AiVideoGenerationRequest(
            prompt="根据这段视频继续生成",
            reference_video_urls=(
                "https://hiagent.example.com/api/proxy/down?Path=upload%2Ffull%2Fclip",
            ),
            duration_seconds=5,
            ratio="adaptive",
            resolution="480p",
        )
    )
    video_payload = client.calls[1][1]
    assert video_payload["Prompt"] == "根据这段视频继续生成"
    assert video_payload["Files"][0]["Role"] == "reference_video"
    assert video_payload["Files"][0]["File"]["Path"] == "upload/full/clip"


def test_hiagent_video_submit_multiple_images_are_all_reference_images():
    client = _FakeHiAgentClient()
    port = HiAgentVideoGenerationPort(
        client=client,
        pro_model="pro-model",
        fast_model="fast-model",
    )
    image_url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
    port.submit(
        AiVideoGenerationRequest(
            prompt="根据这几张图生成视频",
            image_url=image_url,
            reference_image_urls=(image_url, image_url),
            duration_seconds=5,
            ratio="adaptive",
            resolution="480p",
        )
    )
    files = client.calls[0][1]["Files"]
    assert client.calls[0][1]["Prompt"] == "根据这几张图生成视频"
    assert [item["Role"] for item in files] == [
        "reference_image",
        "reference_image",
        "reference_image",
    ]
    assert all(not item["File"]["Url"].startswith("data:") for item in files)


def test_hiagent_video_get_matches_requested_task_id():
    client = _FakeHiAgentClient()
    port = HiAgentVideoGenerationPort(
        client=client,
        pro_model="pro-model",
        fast_model="fast-model",
    )
    task = port.get("task-wanted")
    assert task.status == "succeeded"
    assert task.video_url == "https://x/new.mp4"


def test_hiagent_video_get_reads_error_code_and_lowercase_id():
    class _FailedClient(_FakeHiAgentClient):
        def _request(self, action, body, timeout=None):
            return {
                "Tasks": [
                    {
                        "id": "task-wanted",
                        "Status": "failed",
                        "Error": {"Code": "InvalidParameter", "Message": "bad files"},
                    }
                ]
            }

    port = HiAgentVideoGenerationPort(
        client=_FailedClient(),
        pro_model="pro-model",
        fast_model="fast-model",
    )
    task = port.get("task-wanted")
    assert task.status == "failed"
    assert task.error_code == "InvalidParameter"
    assert task.error_message == "bad files"
