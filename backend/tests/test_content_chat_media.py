"""Chat media uses actual adapter payloads without contacting paid providers."""

import base64
import hashlib
import io
import json
import socket
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from content_service import chat_media
from content_service.config import load_settings
from content_service.llm_adapters import ProviderCallError
from content_service.providers import AiVideoTask


@pytest.fixture(autouse=True)
def public_dns_without_network(monkeypatch):
    monkeypatch.delenv("HIAGENT_RESULT_BASE_URLS", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )


@pytest.fixture
def settings(tmp_path):
    return replace(
        load_settings(),
        data_dir=tmp_path,
        image_provider="volcengine_ark",
        image_provider_id="ark-image",
        image_base_url="https://ark.example/api/v3",
        image_api_key="private-image-key",
        image_model="old-image",
        ai_video_provider="volcengine_ark",
        ai_video_provider_id="ark-video",
        ai_video_base_url="https://ark.example/api/v3",
        ai_video_api_key="private-video-key",
        ai_video_model="old-video",
        ai_video_supported_durations=(5, 10),
        ai_video_supported_ratios=("16:9", "1:1"),
        ai_video_supported_resolutions=("720p", "1080p"),
        hiagent_access_key="private-ak",
        hiagent_secret_key="private-sk",
        hiagent_workspace_id="ws-test",
    )


@pytest.fixture
def png():
    with io.BytesIO() as buffer:
        Image.new("RGB", (20, 20), "red").save(buffer, "PNG")
        return buffer.getvalue()


def model(kind="image", *, edit=False):
    return {
        "kind": kind,
        "provider_source": kind,
        "model": "selected-model",
        "supports_image_edit": edit,
    }


def output(settings):
    return settings.data_dir / "private" / "chat" / "artifacts" / "result"


def reference(settings, content):
    source = settings.data_dir / "private" / "chat" / "attachments" / "reference.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


class FakeHTTP:
    def __init__(self, *, image=None, submit=None, get=None, media=b"", mime="video/mp4"):
        self.image = image
        self.submit_result = submit
        self.get_result = get
        self.media = media
        self.mime = mime
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return httpx.Response(
            200, request=httpx.Request("POST", url), json=self.image or self.submit_result
        )

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return httpx.Response(200, request=httpx.Request("GET", url), json=self.get_result)

    def stream(self, method, url):
        self.calls.append(("STREAM", url, {}))
        response = httpx.Response(
            200,
            request=httpx.Request(method, url),
            headers={"content-type": self.mime},
            content=self.media,
        )

        class Context:
            def __enter__(self):
                return response

            def __exit__(self, *args):
                return None

        return Context()


class FakeHiAgent:
    workspace_id = "ws-test"
    timeout = 180

    def __init__(self):
        self.calls = []
        self.uploaded = []

    def list_granted_models(self):
        return [{"ID": "selected-model"}]

    def upload_raw(self, content, *, content_type, filename):
        self.uploaded.append((content, content_type, filename))
        return {
            "Path": f"upload/full/{filename}",
            "Size": len(content),
            "Url": f"https://hiagent.example.com/down?Path=upload/full/{filename}",
        }

    def _request(self, action, body, **kwargs):
        self.calls.append((action, body))
        if action in {"ImageGen", "CreateImageEdit"}:
            return {"Data": [{"Url": "https://cdn.example/result.png"}]}
        if action == "CreateVideoGenTask":
            return {"task_id": "task-1"}
        if action == "GetVideoGenTask":
            return {
                "Tasks": [
                    {
                        "TaskID": "task-1",
                        "Status": "success",
                        "Model": "selected-model",
                        "Data": {
                            "VideoUrl": "https://cdn.example/result.mp4",
                            "Url": "https://cdn.example/result.mp4",
                        },
                    }
                ]
            }
        raise AssertionError(action)


def assert_artifact(result, *, kind, content):
    assert result["kind"] == kind
    assert result["path"].is_file()
    assert result["path"].read_bytes() == content
    assert result["name"] == result["path"].name
    assert result["size"] == len(content)
    assert result["sha256"] == hashlib.sha256(content).hexdigest()


def test_ark_image_generation_and_reference_payload(settings, png, monkeypatch):
    http = FakeHTTP(image={"data": [{"b64_json": base64.b64encode(png).decode()}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    result = chat_media.generate_chat_media(
        settings,
        model(edit=True),
        prompt="把瓶身背景改成金色",
        output_dir=output(settings),
        reference_path=reference(settings, png),
    )
    assert_artifact(result, kind="image", content=png)
    assert result["mime_type"] == "image/png"
    payload = http.calls[0][2]["json"]
    assert payload["model"] == "selected-model"
    assert payload["prompt"] == "把瓶身背景改成金色"
    assert payload["image"] == ["data:image/png;base64," + base64.b64encode(png).decode()]
    assert http.calls[0][1] == "https://ark.example/api/v3/images/generations"


def test_ark_image_without_reference_has_generation_payload(settings, png, monkeypatch):
    http = FakeHTTP(image={"data": [{"b64_json": base64.b64encode(png).decode()}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    chat_media.generate_chat_media(
        settings, model(), prompt="产品海报", output_dir=output(settings)
    )
    assert "image" not in http.calls[0][2]["json"]


def test_hiagent_real_image_upload_and_edit_payload(settings, png, monkeypatch):
    settings = replace(settings, image_provider="hiagent")
    client = FakeHiAgent()
    monkeypatch.setattr(chat_media, "hiagent_client", lambda *args: client)
    downloaded = []
    monkeypatch.setattr(
        chat_media, "_download_image", lambda url, **kwargs: downloaded.append(url) or png
    )
    result = chat_media.generate_chat_media(
        settings,
        model(edit=True),
        prompt="保持瓶身，替换背景",
        output_dir=output(settings),
        reference_path=reference(settings, png),
    )
    assert_artifact(result, kind="image", content=png)
    assert client.uploaded[0][0] == png
    assert client.uploaded[0][1:] == ("image/png", "reference-1.png")
    action, payload = client.calls[0]
    assert action == "CreateImageEdit"
    assert payload["ModelServiceID"] == "selected-model"
    assert payload["Prompt"] == "保持瓶身，替换背景"
    assert payload["ImageFiles"][0]["Path"] == "upload/full/reference-1.png"
    assert payload["ImageFiles"][0]["Url"].startswith("https://")
    assert downloaded == ["https://cdn.example/result.png"]


def test_hiagent_text_to_image_uses_generation_action(settings, png, monkeypatch):
    settings = replace(settings, image_provider="hiagent")
    client = FakeHiAgent()
    monkeypatch.setattr(chat_media, "hiagent_client", lambda *args: client)
    monkeypatch.setattr(chat_media, "_download_image", lambda *args, **kwargs: png)
    chat_media.generate_chat_media(
        settings, model(), prompt="新品海报", output_dir=output(settings)
    )
    assert client.calls[0][0] == "ImageGen"
    assert "ImageFiles" not in client.calls[0][1]


def test_ark_video_reference_and_private_download(settings, png, monkeypatch):
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
    http = FakeHTTP(
        submit={"id": "task-1", "status": "queued"},
        get={
            "id": "task-1",
            "status": "succeeded",
            "content": {"video_url": "https://cdn.example/result.mp4"},
        },
        media=mp4,
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    result = chat_media.generate_chat_media(
        settings,
        model("video"),
        prompt="瓶身缓慢旋转",
        output_dir=output(settings),
        reference_path=reference(settings, png),
    )
    assert_artifact(result, kind="video", content=mp4)
    assert result["mime_type"] == "video/mp4"
    payload = http.calls[0][2]["json"]
    assert payload["model"] == "selected-model"
    assert payload["content"][0] == {"type": "text", "text": "瓶身缓慢旋转"}
    assert payload["content"][1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(png).decode()
    )
    assert payload["duration"] == 5
    assert payload["ratio"] == "16:9"
    assert payload["resolution"] == "720p"
    assert payload["generate_audio"] is True
    assert http.calls[-1][:2] == ("STREAM", "https://cdn.example/result.mp4")


def test_hiagent_video_uploads_reference_and_uses_exact_model(settings, png, monkeypatch):
    settings = replace(settings, ai_video_provider="hiagent", hiagent_video_fallback_model="other")
    client = FakeHiAgent()
    monkeypatch.setattr(chat_media, "hiagent_client", lambda *args: client)
    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
    http = FakeHTTP(media=mp4)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    result = chat_media.generate_chat_media(
        settings,
        model("video"),
        prompt="参考图生成短片",
        output_dir=output(settings),
        reference_path=reference(settings, png),
    )
    assert_artifact(result, kind="video", content=mp4)
    assert client.uploaded and client.uploaded[0][1] == "image/jpeg"
    with Image.open(io.BytesIO(client.uploaded[0][0])) as uploaded:
        assert uploaded.width >= 300 and uploaded.height >= 300
        assert uploaded.getpixel((0, 0))[0] > 200
    payload = client.calls[0][1]
    assert client.calls[0][0] == "CreateVideoGenTask"
    assert payload["Model"] == "selected-model"
    assert payload["GenerateAudio"] is True
    assert payload["Files"][0]["Role"] == "first_frame"
    assert payload["Files"][0]["File"]["Path"].startswith("upload/full/")


@pytest.mark.parametrize("provider", ["hiagent", "volcengine_ark"])
def test_video_explicit_silence_reaches_actual_provider_payload(settings, monkeypatch, provider):
    settings = replace(settings, ai_video_provider=provider)
    client = FakeHiAgent()
    monkeypatch.setattr(chat_media, "hiagent_client", lambda *args: client)
    http = FakeHTTP(
        submit={"id": "task-1", "status": "succeeded", "content": {"video_url": "https://cdn.example/result.mp4"}},
        media=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20,
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    chat_media.generate_chat_media(settings, model("video"), prompt="生成静音视频", output_dir=output(settings))
    if provider == "hiagent":
        assert client.calls[0][1]["GenerateAudio"] is False
    else:
        assert http.calls[0][2]["json"]["generate_audio"] is False


def test_admin_override_is_applied_before_selected_model(settings, png, monkeypatch):
    path = settings.data_dir / "private" / "ai-models.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "revision": 1,
                "previous": {},
                "events": [],
                "overrides": {
                    "image": {
                        "image_provider": "volcengine_ark",
                        "image_model": "admin-default",
                        "image_base_url": "https://configured.example/api/v3",
                        "image_api_key": "admin-image-key",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    http = FakeHTTP(image={"data": [{"b64_json": base64.b64encode(png).decode()}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    chat_media.generate_chat_media(settings, model(), prompt="海报", output_dir=output(settings))
    assert http.calls[0][1].startswith("https://configured.example/")
    assert http.calls[0][2]["headers"]["Authorization"] == "Bearer admin-image-key"
    assert http.calls[0][2]["json"]["model"] == "selected-model"


@pytest.mark.parametrize("target", ["reference", "output"])
def test_cannot_read_or_write_outside_chat(settings, png, monkeypatch, target):
    monkeypatch.setattr(chat_media, "_image_provider", lambda *args: pytest.fail("provider called"))
    outside = settings.data_dir / "outside.png"
    outside.write_bytes(png)
    with pytest.raises(ValueError, match="私有对话"):
        chat_media.generate_chat_media(
            settings,
            model(edit=True),
            prompt="编辑图片",
            output_dir=outside.parent if target == "output" else output(settings),
            reference_path=outside if target == "reference" else None,
        )


def test_unsupported_edit_rejects_before_provider(settings, png, monkeypatch):
    monkeypatch.setattr(chat_media, "_image_provider", lambda *args: pytest.fail("provider called"))
    with pytest.raises(chat_media.ChatMediaError, match="未开放参考图编辑"):
        chat_media.generate_chat_media(
            settings,
            model(),
            prompt="编辑",
            output_dir=output(settings),
            reference_path=reference(settings, png),
        )


def test_local_mock_is_not_reported_as_real_media(settings):
    with pytest.raises(ValueError, match="不支持"):
        chat_media.generate_chat_media(
            replace(settings, image_provider="mock"),
            model(),
            prompt="海报",
            output_dir=output(settings),
        )


def test_invalid_reference_rejects_before_provider(settings, monkeypatch):
    monkeypatch.setattr(chat_media, "_image_provider", lambda *args: pytest.fail("provider called"))
    with pytest.raises(ValueError, match="无法解码"):
        chat_media.generate_chat_media(
            settings,
            model(edit=True),
            prompt="编辑",
            output_dir=output(settings),
            reference_path=reference(settings, b"not an image"),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/a.png",
        "file:///etc/passwd",
        "https://localhost/a.png",
        "https://127.0.0.1/a.png",
        "https://10.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://user:password@cdn.example/a.png",
        "https://cdn.example/a.png#fragment",
    ],
)
def test_image_url_rejected_before_http(url, monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("HTTP called"))
    with pytest.raises(ProviderCallError):
        chat_media._download_image(url, timeout_seconds=1)


def test_image_url_dns_cannot_resolve_to_private_ip(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))],
    )
    with pytest.raises(ProviderCallError, match="非公网"):
        chat_media._safe_image_url("https://cdn.example/a.png")


def test_image_url_download_and_content_validation(settings, png, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    http = FakeHTTP(
        image={"data": [{"url": "https://cdn.example/a.png"}]}, media=png, mime="image/png"
    )
    options = []
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: options.append(kwargs) or http)
    result = chat_media.generate_chat_media(
        settings, model(), prompt="图片", output_dir=output(settings)
    )
    assert_artifact(result, kind="image", content=png)
    assert all(option["follow_redirects"] is False for option in options)


def test_invalid_generated_image_does_not_create_artifact(settings, monkeypatch):
    http = FakeHTTP(image={"data": [{"b64_json": base64.b64encode(b"bad file").decode()}]})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    with pytest.raises(ProviderCallError, match="图片文件无效"):
        chat_media.generate_chat_media(
            settings, model(), prompt="图片", output_dir=output(settings)
        )
    assert not list(output(settings).iterdir())


@pytest.mark.parametrize("status", ["failed", "expired", "cancelled"])
def test_failed_video_is_retryable_message_and_never_downloaded(settings, monkeypatch, status):
    class Provider:
        def submit(self, request):
            return AiVideoTask(
                "task-1",
                status,
                "video",
                "selected-model",
                error_message="temporary failure https://secret.example/?token=credential",
            )

    monkeypatch.setattr(chat_media, "_video_provider", lambda *args: Provider())
    monkeypatch.setattr(
        chat_media, "download_generated_video", lambda *args, **kwargs: pytest.fail("download")
    )
    with pytest.raises(ProviderCallError, match="请重试") as error:
        chat_media.generate_chat_media(
            settings, model("video"), prompt="视频", output_dir=output(settings)
        )
    assert "credential" not in str(error.value)


def test_video_timeout_caps_wait_and_does_not_download(settings, monkeypatch):
    settings = replace(settings, ai_video_max_wait_seconds=2000)

    class Provider:
        def submit(self, request):
            return AiVideoTask("task-1", "queued", "video", "selected-model")

    def wait(*args, **kwargs):
        assert kwargs["max_wait_seconds"] == 900
        raise TimeoutError()

    monkeypatch.setattr(chat_media, "_video_provider", lambda *args: Provider())
    monkeypatch.setattr(chat_media, "wait_for_ai_video_task", wait)
    with pytest.raises(ProviderCallError, match="等待超时") as error:
        chat_media.generate_chat_media(
            settings, model("video"), prompt="视频", output_dir=output(settings)
        )
    assert error.value.retryable is True


def test_video_lower_configured_wait_and_download_limit(settings, monkeypatch):
    settings = replace(settings, ai_video_max_wait_seconds=60)

    class Provider:
        def submit(self, request):
            return AiVideoTask("task-1", "queued", "video", "selected-model")

    def wait(*args, **kwargs):
        assert kwargs["max_wait_seconds"] == 60
        return AiVideoTask(
            "task-1",
            "succeeded",
            "video",
            "selected-model",
            video_url="https://cdn.example/result.mp4",
        )

    def download(*args, **kwargs):
        assert kwargs["max_bytes"] == 100 * 1024 * 1024
        raise ProviderCallError("download unavailable")

    monkeypatch.setattr(chat_media, "_video_provider", lambda *args: Provider())
    monkeypatch.setattr(chat_media, "wait_for_ai_video_task", wait)
    monkeypatch.setattr(chat_media, "download_generated_video", download)
    with pytest.raises(ProviderCallError, match="download unavailable"):
        chat_media.generate_chat_media(
            settings, model("video"), prompt="视频", output_dir=output(settings)
        )
    assert not list(output(settings).iterdir())


def test_video_task_identifier_cannot_inject_a_provider_path(settings, monkeypatch):
    class Provider:
        def submit(self, request):
            return AiVideoTask(
                "../other-endpoint?secret=private", "queued", "video", "selected-model"
            )

    monkeypatch.setattr(chat_media, "_video_provider", lambda *args: Provider())
    monkeypatch.setattr(
        chat_media, "wait_for_ai_video_task", lambda *args, **kwargs: pytest.fail("poll called")
    )
    with pytest.raises(ProviderCallError, match="任务标识"):
        chat_media.generate_chat_media(
            settings, model("video"), prompt="视频", output_dir=output(settings)
        )


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"content-type": "text/html"}, 200),
        ({"content-type": "image/png", "content-length": "31457281"}, 200),
        ({"content-type": "image/png", "content-length": "invalid"}, 200),
        ({"location": "https://127.0.0.1/a.png"}, 302),
    ],
)
def test_image_download_rejects_type_size_and_redirect(headers, status, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    response = httpx.Response(
        status,
        request=httpx.Request("GET", "https://cdn.example/a.png"),
        headers=headers,
        content=b"not downloaded",
    )

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def stream(self, *args):
            class Context:
                def __enter__(self):
                    return response

                def __exit__(self, *args):
                    return None

            return Context()

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: Client())
    with pytest.raises(ProviderCallError):
        chat_media._download_image("https://cdn.example/a.png", timeout_seconds=1)


def test_video_result_must_not_be_html(settings, monkeypatch):
    http = FakeHTTP(
        submit={
            "id": "task-1",
            "status": "succeeded",
            "content": {"video_url": "https://cdn.example/result.mp4"},
        },
        media=b"<html>error</html>",
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    with pytest.raises(ProviderCallError, match="格式无效"):
        chat_media.generate_chat_media(
            settings, model("video"), prompt="视频", output_dir=output(settings)
        )
    assert not list(output(settings).iterdir())


@pytest.mark.parametrize("kind,edit", [("image", False), ("image", True), ("video", False), ("video", True)])
@pytest.mark.parametrize("result_service", ["signed", "generated_objects"])
def test_hiagent_enterprise_result_download_end_to_end(settings, png, monkeypatch, kind, edit, result_service):
    settings = replace(
        settings,
        image_provider="hiagent",
        ai_video_provider="hiagent",
        hiagent_base_url="https://hiagent.example.com:30040",
    )
    result_url = (
        "https://hiagent.example.com/api/proxy/down"
        "?Action=DownloadForPresign&Key=test-key&Path=generated%2Fresult"
    )
    if result_service == "generated_objects":
        extension = "png" if kind == "image" else "mp4"
        result_url = f"https://hiagent.example.com:30873/bucket/2026/10/result.{extension}"
    client = FakeHiAgent()
    original_request = client._request

    def provider_request(action, body, **kwargs):
        result = original_request(action, body, **kwargs)
        if action in {"ImageGen", "CreateImageEdit"}:
            result["Data"][0]["Url"] = result_url
        elif action == "GetVideoGenTask":
            result["Tasks"][0]["Data"] = {"VideoUrl": result_url, "Url": result_url}
        return result

    monkeypatch.setattr(client, "_request", provider_request)
    monkeypatch.setattr(chat_media, "hiagent_client", lambda *args: client)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.2.19", 30873 if result_service == "generated_objects" else 443))],
    )
    content = png if kind == "image" else b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
    http = FakeHTTP(media=content, mime="image/png" if kind == "image" else "video/mp4")
    options = []
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: options.append(kwargs) or http)
    result = chat_media.generate_chat_media(
        settings,
        model(kind, edit=edit),
        prompt="企业文件结果",
        output_dir=output(settings),
        reference_path=reference(settings, png) if edit else None,
    )
    assert_artifact(result, kind=kind, content=content)
    assert http.calls == [("STREAM", result_url, {})]
    assert all(option["follow_redirects"] is False for option in options)
    assert options[0]["trust_env"] is False


def test_ark_cannot_inherit_hiagent_enterprise_download_trust(settings, png, monkeypatch):
    result_url = (
        "https://hiagent.example.com/api/proxy/down"
        "?Action=DownloadForPresign&Key=test-key&Path=generated%2Fresult"
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.2.99", 443))],
    )
    http = FakeHTTP(image={"data": [{"url": result_url}]}, media=png, mime="image/png")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: http)
    with pytest.raises(ProviderCallError, match="非公网"):
        chat_media.generate_chat_media(settings, model(), prompt="图片", output_dir=output(settings))
    assert len(http.calls) == 1
    assert http.calls[0][0] == "POST"
