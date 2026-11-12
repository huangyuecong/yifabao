import base64
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from content_service.hiagent_adapters import (
    HiAgentClient,
    HiAgentImageGenerationPort,
    HiAgentVideoGenerationPort,
    _absolute_public_url,
    _decode_data_url,
    _ensure_min_image_edge,
    _filename_from_url,
    _parse_image_size,
    _read_file_uri,
    _uploaded_path_from_url,
    prepare_video_file_input,
)
from content_service.llm_adapters import ProviderCallError
from content_service.providers import (
    AiVideoGenerationRequest,
    ImageGenerationRequest,
)

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _FakeClient:
    workspace_id = "ws-test"

    def __init__(self, payload=None, *, models=None, error=None):
        self.payload = payload or {}
        self.models = models or [{"ID": "pro-model", "Granted": True}]
        self.error = error
        self.calls = []
        self.uploaded = []

    def list_granted_models(self):
        if self.error:
            raise self.error
        return self.models

    def upload_raw(self, content, *, content_type, filename):
        self.uploaded.append((filename, content_type, len(content)))
        return {
            "Path": f"upload/full/{filename}",
            "Size": len(content),
            "Url": (
                "https://hiagent.example.com/api/proxy/down"
                f"?Action=DownloadForPresign&Path=upload/full/{filename}"
            ),
        }

    def _request(self, action, body, timeout=None):
        if self.error:
            raise self.error
        self.calls.append((action, body, timeout))
        return self.payload


def test_hiagent_client_rejects_empty_credentials():
    with pytest.raises(ValueError, match="不能为空"):
        HiAgentClient(access_key="", secret_key="sk", workspace_id="ws")


def test_hiagent_client_wraps_quickstart_failures(monkeypatch):
    client = HiAgentClient(access_key="ak", secret_key="sk", workspace_id="ws")

    def boom(*_args, **_kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr("content_service.hiagent_quickstart.call", boom)
    with pytest.raises(ProviderCallError, match="gateway down"):
        client.list_granted_models()

    monkeypatch.setattr("content_service.hiagent_quickstart.upload_raw", boom)
    with pytest.raises(ProviderCallError, match="gateway down"):
        client.upload_raw(b"x", content_type="image/png", filename="a.png")


def test_parse_image_size_presets_and_custom_pixels():
    assert _parse_image_size("2K", "16:9") == (2560, 1440)
    assert _parse_image_size("3K", "1:1") == (3072, 3072)
    assert _parse_image_size("4K", "9:16") == (2304, 4096)
    assert _parse_image_size("2048x2048", None) == (2048, 2048)
    assert _parse_image_size(None, "2048:2048") == (2048, 2048)
    assert _parse_image_size(2048, "1:1") == (2048, 2048)
    assert _parse_image_size(None, "21:9")[0] >= 3136
    with pytest.raises(ValueError, match="总像素"):
        _parse_image_size("480x480", None)


def test_reference_url_helpers(tmp_path: Path):
    assert _uploaded_path_from_url("http://example.com/?Path=upload/full/a") is None
    assert (
        _uploaded_path_from_url("https://hiagent.example.com/api/proxy/down?Path=upload/full/a.png")
        == "upload/full/a.png"
    )
    assert _filename_from_url("https://example.com/down", "fallback.png") == "fallback.png"
    assert _filename_from_url("https://example.com/clip.mp4", "fallback.png") == "clip.mp4"
    assert _absolute_public_url("https://cdn.example/a.png") == "https://cdn.example/a.png"
    assert _absolute_public_url("/api/proxy/down") == "https://hiagent.example.com/api/proxy/down"

    with pytest.raises(ValueError, match="Data URL"):
        _decode_data_url("not-a-data-url")
    payload, media_type = _decode_data_url(
        "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
    )
    assert media_type == "image/png"
    assert payload == TINY_PNG

    missing = tmp_path / "missing.png"
    with pytest.raises(ValueError, match="不存在"):
        _read_file_uri(missing.as_uri())
    source = tmp_path / "poster.png"
    source.write_bytes(TINY_PNG)
    content, media_type = _read_file_uri(source.as_uri())
    assert content == TINY_PNG
    assert media_type == "image/png"


def test_ensure_min_image_edge_upscales_tiny_png():
    content, media_type = _ensure_min_image_edge(TINY_PNG, "image/png")
    with Image.open(BytesIO(content)) as image:
        assert min(image.size) >= 300
    assert media_type in {"image/png", "image/jpeg"}


def test_prepare_video_file_input_existing_path_and_unsupported_scheme():
    existing = prepare_video_file_input(
        "https://hiagent.example.com/api/proxy/down?Path=upload/full/kept.png",
        name="kept.png",
        kind="image",
        role="first_frame",
        uploader=lambda *_args, **_kwargs: pytest.fail("should not upload"),
    )
    assert existing["File"]["Path"] == "upload/full/kept.png"
    with pytest.raises(ValueError, match="不受支持"):
        prepare_video_file_input(
            "ftp://example.com/a.png",
            name="a.png",
            kind="image",
            role="first_frame",
            uploader=lambda *_args, **_kwargs: {},
        )


def test_prepare_video_file_input_uploads_local_file(tmp_path: Path):
    source = tmp_path / "frame.png"
    Image.new("RGB", (320, 320), "red").save(source)
    uploaded = {}

    def uploader(content, *, content_type, filename):
        uploaded["size"] = len(content)
        return {
            "Path": "upload/full/frame.png",
            "Size": len(content),
            "Url": "https://hiagent.example.com/api/proxy/down?Path=upload/full/frame.png",
        }

    result = prepare_video_file_input(
        source.as_uri(),
        name="frame.png",
        kind="image",
        role="first_frame",
        uploader=uploader,
    )
    assert result["File"]["Path"] == "upload/full/frame.png"
    assert uploaded["size"] > 0


def test_image_port_rejects_invalid_requests_and_downloads_assets(monkeypatch):
    client = _FakeClient({"Data": [{"Url": "https://cdn.example/a.png"}]})
    port = HiAgentImageGenerationPort(client=client, model_service_id="seedream")
    with pytest.raises(ValueError, match="提示词不能为空"):
        port.generate(ImageGenerationRequest(prompt="   "))
    with pytest.raises(ValueError, match="候选数量"):
        port.generate(ImageGenerationRequest(prompt="臻品", count=9))

    class _Response:
        content = TINY_PNG

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: _Response())
    assets = port.generate(ImageGenerationRequest(prompt="臻品雅酿", size="2K", ratio="1:1"))
    assert assets and assets[0].extension == "png"
    assert client.calls[0][0] == "ImageGen"


def test_image_port_edits_with_reference_images(monkeypatch):
    client = _FakeClient({"Data": [{"Url": "https://cdn.example/a.jpg"}]})
    port = HiAgentImageGenerationPort(client=client, model_service_id="seedream")
    jpeg = b"\xff\xd8\xff" + b"payload"

    class _Response:
        content = jpeg

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: _Response())
    assets = port.generate(
        ImageGenerationRequest(
            prompt="参考图改写",
            reference_images=(TINY_PNG,),
            reference_media_types=("image/png",),
        )
    )
    assert assets[0].extension == "jpg"
    assert client.uploaded == [("reference-1.png", "image/png", len(TINY_PNG))]
    assert client.calls[0][0] == "CreateImageEdit"
    image_files = client.calls[0][1]["ImageFiles"]
    assert image_files[0]["Path"] == "upload/full/reference-1.png"
    assert image_files[0]["Url"].startswith("https://")
    assert not image_files[0]["Url"].startswith("data:")


def test_image_port_converts_webp_reference_before_edit(monkeypatch):
    client = _FakeClient({"Data": [{"Url": "https://cdn.example/a.png"}]})
    port = HiAgentImageGenerationPort(client=client, model_service_id="seedream")
    buffer = BytesIO()
    Image.new("RGB", (320, 320), "red").save(buffer, format="WEBP")

    class _Response:
        content = TINY_PNG

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: _Response())
    assets = port.generate(
        ImageGenerationRequest(
            prompt="参考图改写",
            reference_images=(buffer.getvalue(),),
            reference_media_types=("image/webp",),
        )
    )
    assert assets
    assert client.uploaded[0][1] in {"image/jpeg", "image/png"}
    assert client.calls[0][0] == "CreateImageEdit"
    assert not client.calls[0][1]["ImageFiles"][0]["Url"].startswith("data:")


def test_image_port_requires_returned_images():
    port = HiAgentImageGenerationPort(
        client=_FakeClient({"Data": []}),
        model_service_id="seedream",
    )
    with pytest.raises(ProviderCallError, match="未返回图片"):
        port.generate(ImageGenerationRequest(prompt="臻品"))


@pytest.mark.parametrize("with_reference", [False, True])
def test_image_port_requests_each_candidate_and_reuses_uploaded_references(
    monkeypatch, with_reference,
):
    class SingleImageClient(_FakeClient):
        def _request(self, action, body, timeout=None):
            self.calls.append((action, body, timeout))
            return {"Data": [{"Url": f"https://cdn.example/{len(self.calls)}.png"}]}

    client = SingleImageClient()
    provider = HiAgentImageGenerationPort(client=client, model_service_id="seedream", max_candidates=8)
    downloads = []

    def download(url, **_kwargs):
        downloads.append(url)
        return httpx.Response(200, content=TINY_PNG + url.encode(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", download)
    assets = provider.generate(ImageGenerationRequest(
        prompt="产品展示", count=3,
        reference_images=(TINY_PNG,) if with_reference else (),
        reference_media_types=("image/png",) if with_reference else (),
    ))
    assert len(assets) == 3
    assert len({asset.content for asset in assets}) == 3
    assert len(downloads) == 3
    assert [body["MaxImages"] for _, body, _ in client.calls] == [1, 1, 1]
    assert {action for action, _, _ in client.calls} == {
        "CreateImageEdit" if with_reference else "ImageGen"
    }
    assert len(client.uploaded) == int(with_reference)
    assert [asset.metadata["candidate_index"] for asset in assets] == [0, 1, 2]


def test_image_port_does_not_return_partial_batch_or_duplicate_on_failure(monkeypatch):
    class FailedSecondCandidate(_FakeClient):
        def _request(self, action, body, timeout=None):
            self.calls.append((action, body, timeout))
            if len(self.calls) == 2:
                return {"Data": []}
            return {"Data": [{"Url": "https://cdn.example/1.png"}]}

    client = FailedSecondCandidate()
    monkeypatch.setattr(httpx, "get", lambda url, **_kwargs: httpx.Response(
        200, content=TINY_PNG, request=httpx.Request("GET", url),
    ))
    provider = HiAgentImageGenerationPort(client=client, model_service_id="seedream", max_candidates=8)
    with pytest.raises(ProviderCallError, match="未返回图片"):
        provider.generate(ImageGenerationRequest(prompt="产品展示", count=3))
    assert len(client.calls) == 2


def test_video_port_validates_and_submits_with_fast_fallback():
    client = _FakeClient({"task_id": "task-1"}, models=[{"ID": "fast-model"}])
    port = HiAgentVideoGenerationPort(
        client=client,
        pro_model="pro-model",
        fast_model="fast-model",
        durations=(5, 6, 7),
        ratios=("16:9", "adaptive"),
    )
    with pytest.raises(ValueError, match="提示词不能为空"):
        port.submit(AiVideoGenerationRequest(prompt=" "))
    with pytest.raises(ValueError, match="时长"):
        port.submit(AiVideoGenerationRequest(prompt="臻品", duration_seconds=99))
    with pytest.raises(ValueError, match="比例"):
        port.submit(AiVideoGenerationRequest(prompt="臻品", duration_seconds=5, ratio="2:1"))
    with pytest.raises(ValueError, match="暂不支持取消"):
        port.cancel("task-1")

    task = port.submit(
        AiVideoGenerationRequest(prompt="臻品短片", duration_seconds=5, resolution="1080p")
    )
    assert task.provider_task_id == "task-1"
    assert task.metadata["downgraded"] is True
    assert task.metadata["resolution"] == "720p"
    assert client.calls[-1][1]["Model"] == "fast-model"


def test_video_port_prefers_pro_and_retries_fast_with_same_files():
    client = _FakeClient(
        {"task_id": "task-2"},
        models=[{"ID": "pro-model", "Granted": True}, {"ID": "fast-model", "Granted": True}],
    )
    port = HiAgentVideoGenerationPort(
        client=client,
        pro_model="pro-model",
        fast_model="fast-model",
        durations=(5,),
        ratios=("16:9",),
    )
    image_url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")
    submitted = port.submit(
        AiVideoGenerationRequest(
            prompt="根据参考图生成视频",
            image_url=image_url,
            duration_seconds=5,
            resolution="720p",
            ratio="16:9",
        )
    )
    assert client.calls[0][0] == "CreateVideoGenTask"
    assert client.calls[0][1]["Model"] == "pro-model"
    assert client.calls[0][1]["Prompt"] == "根据参考图生成视频"
    assert client.calls[0][1]["Files"][0]["Role"] == "first_frame"
    retried = port.submit_fast_retry(submitted.metadata["retry_payload"])
    assert retried.model == "fast-model"
    assert client.calls[-1][1]["Model"] == "fast-model"
    assert client.calls[-1][1]["Files"] == client.calls[0][1]["Files"]


def test_video_port_get_maps_status_and_missing_task():
    client = _FakeClient(
        {
            "Tasks": [
                {"TaskID": "other", "Status": "failed"},
                {
                    "TaskID": "wanted",
                    "Status": "success",
                    "Model": "fast-model",
                    "Data": [{"Url": "https://cdn.example/out.mp4"}],
                },
            ]
        }
    )
    port = HiAgentVideoGenerationPort(
        client=client,
        pro_model="pro-model",
        fast_model="fast-model",
        durations=(5,),
        ratios=("adaptive",),
    )
    fetched = port.get("wanted")
    assert fetched.status == "succeeded"
    assert fetched.video_url == "https://cdn.example/out.mp4"
    with pytest.raises(ProviderCallError, match="不存在"):
        port.get("missing")
