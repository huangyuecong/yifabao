import base64
import io
import json
import os
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from docx import Document
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CONTENT_SRC = ROOT / "services" / "content_service" / "src"
if str(CONTENT_SRC) not in sys.path:
    sys.path.insert(0, str(CONTENT_SRC))

from content_service import media_adapters
from content_service.config import load_settings
from content_service.knowledge_ingestion import (
    discover_official_jsonl_categories,
    extract_document_text,
    ingest_document_file,
    load_official_jsonl_bundle,
)
from content_service.llm_adapters import ProviderCallError
from content_service.media_adapters import (
    FfmpegVideoComposerPort,
    LocalAssetStore,
    LocalSubtitleAlignmentPort,
    MockSpeechSynthesisPort,
    VolcengineApiKeySpeechSynthesisPort,
    VolcengineArkAiVideoGenerationPort,
    VolcengineArkImageGenerationPort,
    VolcengineSpeechSynthesisPort,
    download_generated_video,
    provider_capability_document,
    render_srt,
    wait_for_ai_video_task,
)
from content_service.provider_health import current_health, track_provider
from content_service.provider_media_gateway import ProviderMediaUrlSigner
from content_service.providers import (
    AiVideoGenerationRequest,
    AiVideoTask,
    ImageGenerationRequest,
    KnowledgeIngestRequest,
    ProviderStatus,
    SpeechSynthesisRequest,
    SubtitleAlignmentRequest,
    SubtitleSegment,
    VideoClip,
    VideoCompositionRequest,
    WordTiming,
)


class FakeResponse:
    def __init__(self, payload=None, *, content=b"", headers=None, status_code=200):
        self.payload = payload
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def iter_lines(self):
        return self.content.decode("utf-8").splitlines()

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, *, post=None, get=None, delete=None):
        self.post_response = post
        self.get_response = get
        self.delete_response = delete
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def post(self, endpoint, **kwargs):
        self.calls.append(("POST", endpoint, kwargs))
        return self.post_response

    def stream(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        return self.post_response

    def get(self, endpoint, **kwargs):
        self.calls.append(("GET", endpoint, kwargs))
        return self.get_response

    def delete(self, endpoint, **kwargs):
        self.calls.append(("DELETE", endpoint, kwargs))
        return self.delete_response


def test_external_provider_health_reflects_calls_and_redacts_sensitive_errors():
    class ExternalProvider:
        status = ProviderStatus(
            provider_id="health-test-provider",
            name="health-test",
            capability="image-generation",
            mode="real",
            external=True,
            enabled=True,
        )
        capabilities: ClassVar[dict] = {}

        def __init__(self):
            self.should_fail = False

        def generate(self):
            if self.should_fail:
                raise RuntimeError(
                    "Authorization: Bearer server-secret url=https://private.example/v1/generate"
                )
            return "ok"

    raw = ExternalProvider()
    provider = track_provider(raw)
    assert current_health(raw.status) is None
    assert provider.generate() == "ok"
    assert current_health(raw.status).status == "available"

    raw.should_fail = True
    with pytest.raises(RuntimeError):
        provider.generate()
    health = current_health(raw.status)
    assert health.status == "unavailable"
    assert "server-secret" not in (health.message or "")
    assert "private.example" not in (health.message or "")
    assert "已隐藏" in (health.message or "")


def test_media_settings_are_explicit_and_subtitle_default_is_not_demo_value(
    monkeypatch,
    tmp_path,
):
    for name in list(os.environ):
        if name.startswith(
            (
                "CONTENT_IMAGE_",
                "CONTENT_SPEECH_",
                "CONTENT_AI_VIDEO_",
                "CONTENT_SUBTITLE_",
                "CONTENT_ARK_",
            )
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    settings = load_settings()
    assert settings.image_provider == "mock"
    assert settings.speech_provider == "mock"
    assert settings.ai_video_provider == "mock"
    assert settings.subtitle_max_chars_per_line != 18

    monkeypatch.setenv("CONTENT_IMAGE_PROVIDER", "volcengine_ark")
    with pytest.raises(ValueError, match="外部图片模型缺少配置"):
        load_settings()


def test_unified_ark_key_configures_text_image_and_video(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(
            (
                "CONTENT_ARK_",
                "CONTENT_LLM_",
                "CONTENT_IMAGE_",
                "CONTENT_AI_VIDEO_",
            )
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTENT_ARK_API_KEY", "one-server-side-key")

    settings = load_settings()

    assert settings.llm_provider == "volcengine_ark"
    assert settings.image_provider == "volcengine_ark"
    assert settings.ai_video_provider == "volcengine_ark"
    assert settings.llm_api_key == "one-server-side-key"
    assert settings.image_api_key == "one-server-side-key"
    assert settings.ai_video_api_key == "one-server-side-key"
    assert settings.llm_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert settings.image_base_url == settings.llm_base_url
    assert settings.ai_video_base_url == settings.llm_base_url
    assert settings.llm_model == "chat-ds-flash"
    assert settings.image_model.startswith("doubao-seedream-")
    assert settings.ai_video_model.startswith("doubao-seedance-")
    assert settings.ai_video_supported_durations == tuple(range(5, 16))
    assert settings.ai_video_supported_ratios == (
        "16:9",
        "4:3",
        "1:1",
        "3:4",
        "9:16",
        "21:9",
        "adaptive",
    )
    assert settings.ai_video_supported_resolutions == ("480p", "720p")
    assert settings.speech_provider == "mock"


def test_speech_api_key_auto_configures_new_speech_without_reusing_ark(
    monkeypatch,
    tmp_path,
):
    for name in list(os.environ):
        if name.startswith(("CONTENT_ARK_", "CONTENT_SPEECH_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTENT_ARK_API_KEY", "ark-only-secret")
    monkeypatch.setenv("CONTENT_SPEECH_API_KEY", "speech-only-secret")
    monkeypatch.setenv("CONTENT_SPEECH_SUPPORTED_VOICES", "custom-voice")

    settings = load_settings()

    assert settings.speech_provider == "volcengine_tts_api_key"
    assert settings.speech_api_key == "speech-only-secret"
    assert settings.speech_api_key != settings.llm_api_key
    assert settings.speech_base_url == "https://openspeech.bytedance.com"
    assert settings.speech_resource_id == "seed-tts-2.0"
    assert settings.speech_voice_type == "zh_female_vv_uranus_bigtts"
    assert settings.speech_supported_voices == (
        "custom-voice",
        "zh_female_vv_uranus_bigtts",
        "zh_male_dayi_saturn_bigtts",
        "zh_female_santongyongns_saturn_bigtts",
    )
    assert "speech-only-secret" not in repr(settings)


def test_ark_image_adapter_uses_server_credentials_and_decodes_candidates(monkeypatch):
    image_bytes = b"valid-image-result"
    client = FakeClient(
        post=FakeResponse(
            {
                "data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}],
                "usage": {"generated_images": 1},
            }
        )
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineArkImageGenerationPort(
        provider_id="ark-image",
        base_url="https://ark.example/api/v3",
        api_key="server-secret",
        model="image-model",
        timeout_seconds=20,
        supported_sizes=("configured-size",),
    )

    result = provider.generate(ImageGenerationRequest("品牌场景", count=2))

    assert result[0].content == image_bytes
    assert len(result) == 2
    assert len(client.calls) == 2
    method, endpoint, request = client.calls[0]
    assert method == "POST"
    assert endpoint == "https://ark.example/api/v3/images/generations"
    assert request["headers"]["Authorization"] == "Bearer server-secret"
    assert request["json"]["response_format"] == "b64_json"
    assert "n" not in request["json"]
    assert provider_capability_document(provider)["parameters"]["sizes"] == ["configured-size"]


def test_ark_image_adapter_sends_reference_images_as_model_media_input(monkeypatch):
    image_bytes = b"valid-image-result"
    reference_bytes = b"\x89PNG\r\n\x1a\n-reference-image"
    client = FakeClient(
        post=FakeResponse(
            {
                "data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}],
            }
        )
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineArkImageGenerationPort(
        provider_id="ark-image",
        base_url="https://ark.example/api/v3",
        api_key="server-secret",
        model="doubao-seedream-5-0-260128",
        timeout_seconds=20,
    )

    provider.generate(
        ImageGenerationRequest(
            "保留主体构图并调整背景",
            reference_images=(reference_bytes,),
            reference_media_types=("image/png",),
        )
    )

    payload = client.calls[0][2]["json"]
    assert payload["image"] == [
        "data:image/png;base64," + base64.b64encode(reference_bytes).decode("ascii")
    ]


def test_modern_seedream_exposes_selectable_and_custom_image_parameters():
    provider = VolcengineArkImageGenerationPort(
        provider_id="ark-image",
        base_url="https://ark.example/api/v3",
        api_key="server-secret",
        model="doubao-seedream-5-0-260128",
        timeout_seconds=20,
    )

    parameters = provider_capability_document(provider)["parameters"]

    assert parameters["sizes"] == ["2K", "3K", "4K"]
    assert parameters["ratios"] == [
        "adaptive",
        "1:1",
        "4:3",
        "3:4",
        "3:2",
        "2:3",
        "16:9",
        "9:16",
        "21:9",
    ]
    assert parameters["custom_size"] is True
    assert parameters["custom_ratio"] is True
    assert parameters["minimum_pixels"] == 3_686_400
    assert parameters["maximum_pixels"] == 16_777_216
    assert parameters["aspect_ratio_range"] == {"min": 1 / 16, "max": 16}
    assert parameters["defaults"] == {"size": "2K", "ratio": "adaptive"}
    assert parameters["parameter_source"] == "provider-contract"


def test_modern_seedream_upscales_undersized_custom_dimensions(monkeypatch):
    image_bytes = b"valid-image-result"
    client = FakeClient(
        post=FakeResponse(
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(image_bytes).decode("ascii"),
                        "size": "2304x1728",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineArkImageGenerationPort(
        provider_id="ark-image",
        base_url="https://ark.example/api/v3",
        api_key="server-secret",
        model="doubao-seedream-5-0-260128",
        timeout_seconds=20,
    )

    result = provider.generate(ImageGenerationRequest("品牌场景", size="1600x1200", ratio="4:3"))

    request_payload = client.calls[0][2]["json"]
    assert request_payload["size"] == "2304x1728"
    assert request_payload["sequential_image_generation"] == "disabled"
    assert result[0].metadata["requested_size"] == "1600x1200"
    assert result[0].metadata["size"] == "2304x1728"


def test_modern_seedream_accepts_3k_and_rejects_oversized_custom_dimensions():
    provider = VolcengineArkImageGenerationPort(
        provider_id="ark-image",
        base_url="https://ark.example/api/v3",
        api_key="server-secret",
        model="doubao-seedream-5-0-260128",
        timeout_seconds=20,
    )

    assert provider._effective_size("3K", "adaptive") == "3K"
    with pytest.raises(ValueError, match="总像素不能超过"):
        provider._effective_size("5000x5000", "1:1")


def test_volcengine_tts_uses_semicolon_bearer_and_parses_audio_timestamps(monkeypatch):
    audio = b"mp3-bytes"
    line = json.dumps(
        {
            "data": base64.b64encode(audio).decode("ascii"),
            "words": [
                {"word": "你", "start_time": 0, "end_time": 250},
                {"word": "好", "start_time": 250, "end_time": 500},
            ],
        }
    ).encode("utf-8")
    client = FakeClient(
        post=FakeResponse(content=line, headers={"content-type": "application/x-ndjson"})
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineSpeechSynthesisPort(
        provider_id="volc-tts",
        base_url="https://speech.example",
        app_id="app-id",
        access_token="speech-secret",
        resource_id="configured-resource",
        default_voice="configured-voice",
        timeout_seconds=30,
    )

    result = provider.synthesize(SpeechSynthesisRequest("你好"))

    assert result.audio == audio
    assert result.duration_seconds == 0.5
    _, endpoint, request = client.calls[0]
    assert endpoint.endswith("/api/v3/tts/unidirectional")
    assert request["headers"]["Authorization"] == "Bearer; speech-secret"
    assert request["headers"]["Resource-Id"] == "configured-resource"
    assert request["json"]["request"]["with_timestamp"] == 1


def test_volcengine_api_key_tts_uses_new_contract_and_provider_timestamps(monkeypatch):
    first_audio = b"first-mp3-chunk"
    second_audio = b"second-mp3-chunk"
    lines = [
        {
            "code": 0,
            "data": base64.b64encode(first_audio).decode("ascii"),
        },
        {
            "code": 0,
            "data": base64.b64encode(second_audio).decode("ascii"),
        },
        {
            "code": 0,
            "data": None,
            "sentence": {
                "text": "你好",
                "words": [
                    {"word": "你", "startTime": 0.05, "endTime": 0.3},
                    {"word": "好", "startTime": 0.3, "endTime": 0.62},
                ],
            },
        },
        {"code": 20000000, "message": "ok", "data": None},
    ]
    response = FakeResponse(
        content=("\n".join(json.dumps(line) for line in lines)).encode("utf-8"),
        headers={"content-type": "application/json", "X-Tt-Logid": "safe-log-id"},
    )
    client = FakeClient(post=response)
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineApiKeySpeechSynthesisPort(
        provider_id="doubao-speech",
        base_url="https://openspeech.bytedance.com",
        api_key="speech-server-secret",
        resource_id="seed-tts-2.0",
        default_voice="zh_female_vv_uranus_bigtts",
        timeout_seconds=30,
    )

    result = provider.synthesize(
        SpeechSynthesisRequest("你好", speed=0.8, volume=1.2, audio_format="mp3")
    )

    assert result.audio == first_audio + second_audio
    assert result.duration_seconds == 0.62
    assert [item.text for item in result.word_timings] == ["你", "好"]
    assert result.metadata["timestamp_source"] == "provider"
    assert result.metadata["provider_log_id"] == "safe-log-id"
    method, endpoint, request = client.calls[0]
    assert method == "POST"
    assert endpoint == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    assert request["headers"]["X-Api-Key"] == "speech-server-secret"
    assert request["headers"]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert request["headers"]["X-Api-Request-Id"]
    assert request["json"]["req_params"]["speaker"] == "zh_female_vv_uranus_bigtts"
    audio_params = request["json"]["req_params"]["audio_params"]
    assert audio_params["speech_rate"] == -20
    assert audio_params["loudness_rate"] == 20
    assert audio_params["enable_subtitle"] is True
    assert "speech-server-secret" not in json.dumps(request["json"])
    assert provider.capabilities["timestamps"] == "provider-word-level"


def test_volcengine_api_key_tts_rejects_business_error_and_wraps_pcm_as_wav(
    monkeypatch,
):
    error_response = FakeResponse(
        content=json.dumps({"code": 55000000, "message": "invalid request"}).encode(),
        headers={"content-type": "application/json"},
    )
    error_client = FakeClient(post=error_response)
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: error_client)
    provider = VolcengineApiKeySpeechSynthesisPort(
        provider_id="doubao-speech",
        base_url="https://openspeech.bytedance.com",
        api_key="speech-server-secret",
        resource_id="seed-tts-2.0",
        default_voice="zh_female_vv_uranus_bigtts",
        timeout_seconds=30,
    )
    with pytest.raises(ProviderCallError, match="55000000"):
        provider.synthesize(SpeechSynthesisRequest("错误测试"))

    pcm = b"\x00\x00\x01\x00\x02\x00\x03\x00"
    success_response = FakeResponse(
        content=(
            json.dumps(
                {
                    "code": 0,
                    "data": base64.b64encode(pcm).decode("ascii"),
                }
            )
            + "\n"
            + json.dumps({"code": 20000000, "message": "ok", "data": None})
        ).encode(),
        headers={"content-type": "application/json"},
    )
    success_client = FakeClient(post=success_response)
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: success_client)
    wav_result = provider.synthesize(
        SpeechSynthesisRequest(
            "WAV 测试",
            audio_format="wav",
            sample_rate=24000,
            with_timestamps=False,
        )
    )
    with wave.open(io.BytesIO(wav_result.audio), "rb") as wav_file:
        assert wav_file.getframerate() == 24000
        assert wav_file.readframes(wav_file.getnframes()) == pcm
    assert wav_result.media_type == "audio/wav"
    assert wav_result.duration_seconds == pytest.approx(4 / 24000)
    assert success_client.calls[0][2]["json"]["req_params"]["audio_params"]["format"] == "pcm"


def test_ark_ai_video_task_contract_and_polling(monkeypatch):
    client = FakeClient(
        post=FakeResponse({"id": "task-1", "status": "queued"}),
        get=FakeResponse(
            {
                "id": "task-1",
                "status": "succeeded",
                "content": {"video_url": "https://media.example/result.mp4"},
            }
        ),
        delete=FakeResponse({"id": "task-1", "status": "cancelled"}),
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineArkAiVideoGenerationPort(
        provider_id="ark-video",
        base_url="https://ark.example/api/v3",
        api_key="server-secret",
        model="video-model",
        timeout_seconds=20,
    )
    submitted = provider.submit(
        AiVideoGenerationRequest(
            "镜头提示",
            image_url="https://assets.example/reference.png",
            reference_image_urls=("https://assets.example/reference-2.png",),
            reference_video_urls=(
                "https://assets.example/reference-1.mp4",
                "https://assets.example/reference-2.mov",
            ),
            reference_video_durations_seconds=(4, 5),
            idempotency_key="request-1",
        )
    )
    completed = wait_for_ai_video_task(
        provider,
        submitted.provider_task_id,
        poll_interval_seconds=0.01,
        max_wait_seconds=1,
        sleep=lambda _: None,
    )

    assert completed.video_url == "https://media.example/result.mp4"
    post = client.calls[0]
    assert post[1].endswith("/contents/generations/tasks")
    assert post[2]["headers"]["Idempotency-Key"] == "request-1"
    assert post[2]["json"]["content"][1]["type"] == "image_url"
    assert post[2]["json"]["content"][1]["role"] == "reference_image"
    assert post[2]["json"]["content"][2]["role"] == "reference_image"
    assert post[2]["json"]["content"][3] == {
        "type": "video_url",
        "video_url": {"url": "https://assets.example/reference-1.mp4"},
        "role": "reference_video",
    }
    assert post[2]["json"]["content"][4]["role"] == "reference_video"
    assert provider.capabilities["reference_images"]["max"] == 9
    assert provider.capabilities["reference_videos"]["max"] == 3
    assert provider.capabilities["reference_videos"]["total_duration_seconds"] == 15


def test_ark_ai_video_privacy_http_error_is_non_retryable_input_rejection():
    request = httpx.Request("POST", "https://ark.example/contents/generations/tasks")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "code": "InputImageSensitiveContentDetected.PrivacyInformation",
                "message": "content[3] may contain real person; request_id=internal-id",
            }
        },
    )
    error = media_adapters._safe_http_error(
        "ark-video",
        httpx.HTTPStatusError(
            "provider rejected request",
            request=request,
            response=response,
        ),
    )

    assert error.code == "input_image_privacy_information"
    assert error.status_code == 422
    assert error.retryable is False
    assert "content[3] may contain real person" in str(error)


def test_ark_ai_video_transient_http_error_preserves_retryability():
    request = httpx.Request("POST", "https://ark.example/contents/generations/tasks")
    response = httpx.Response(
        503,
        request=request,
        json={"error": {"code": "ServiceUnavailable", "message": "try later"}},
    )
    error = media_adapters._safe_http_error(
        "ark-video",
        httpx.HTTPStatusError(
            "provider unavailable",
            request=request,
            response=response,
        ),
    )

    assert error.code == "provider_call_failed"
    assert error.status_code == 503
    assert error.retryable is True


def test_ark_ai_video_rejects_reference_limits_before_http(monkeypatch):
    client = FakeClient(post=FakeResponse({"id": "must-not-submit", "status": "queued"}))
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = VolcengineArkAiVideoGenerationPort(
        provider_id="ark-video",
        base_url="https://ark.example/api/v3",
        api_key="server-secret",
        model="doubao-seedance-2-0-fast-260128",
        timeout_seconds=20,
    )

    with pytest.raises(ValueError, match="最多允许 3"):
        provider.submit(
            AiVideoGenerationRequest(
                "镜头提示",
                reference_video_urls=tuple(
                    f"https://assets.example/reference-{index}.mp4" for index in range(4)
                ),
            )
        )
    with pytest.raises(ValueError, match="最多允许 9"):
        provider.submit(
            AiVideoGenerationRequest(
                "镜头提示",
                reference_image_urls=tuple(
                    f"https://assets.example/reference-{index}.png" for index in range(10)
                ),
            )
        )
    with pytest.raises(ValueError, match="总时长"):
        provider.submit(
            AiVideoGenerationRequest(
                "镜头提示",
                reference_video_urls=(
                    "https://assets.example/reference-1.mp4",
                    "https://assets.example/reference-2.mp4",
                ),
                reference_video_durations_seconds=(8, 8),
            )
        )
    with pytest.raises(ValueError, match="HTTPS URL"):
        provider.submit(
            AiVideoGenerationRequest(
                "镜头提示",
                reference_video_urls=("data:video/mp4;base64,AAAA",),
            )
        )
    monkeypatch.setattr(media_adapters, "_MAX_ARK_AI_VIDEO_REQUEST_BODY_BYTES", 128)
    with pytest.raises(ValueError, match="64MB"):
        provider.submit(
            AiVideoGenerationRequest(
                "镜头提示",
                reference_image_urls=("data:image/png;base64," + "A" * 256,),
            )
        )
    assert client.calls == []


def test_subtitle_alignment_prefers_provider_timestamps_and_estimates_when_absent():
    aligner = LocalSubtitleAlignmentPort(
        default_max_chars_per_line=6,
        default_max_lines=1,
    )
    timed = aligner.align(
        SubtitleAlignmentRequest(
            text="你好世界",
            duration_seconds=2,
            word_timings=(
                WordTiming("你", 0, 0.4),
                WordTiming("好", 0.4, 0.8),
                WordTiming("世", 0.8, 1.2),
                WordTiming("界", 1.2, 1.6),
            ),
        )
    )
    estimated = aligner.align(SubtitleAlignmentRequest("第一句话。第二句话。", duration_seconds=4))

    assert timed[0].timing_source == "provider"
    assert all(item.timing_source == "estimated" for item in estimated)
    assert all(
        current.end_seconds <= following.start_seconds for current, following in pairwise(estimated)
    )
    assert "-->" in render_srt(estimated)


def test_local_asset_store_accepts_parallel_sibling_uploads(tmp_path):
    store = LocalAssetStore(tmp_path / "parallel-objects")
    keys = [f"video-projects/project-a/originals/media-{index}.wav" for index in range(200)]

    with ThreadPoolExecutor(max_workers=16) as executor:
        paths = list(executor.map(store.path_for, keys))

    assert len(paths) == len(keys)
    assert len(set(paths)) == len(keys)
    assert all(store.root in path.parents for path in paths)


def test_local_asset_store_only_exposes_configured_provider_https_urls(tmp_path):
    private_store = LocalAssetStore(tmp_path / "private-objects")
    private_store.put_bytes("项目 A/视频 1.mp4", b"video", "video/mp4")
    assert private_store.provider_url("项目 A/视频 1.mp4") is None

    signer = ProviderMediaUrlSigner(
        base_url="https://objects.example/provider-media",
        signing_secret="provider-media-test-secret-that-is-long-enough",
        ttl_seconds=300,
        clock=lambda: 1_000,
    )
    public_store = LocalAssetStore(
        tmp_path / "public-objects",
        provider_media_url_signer=signer,
    )
    public_store.put_bytes("项目 A/视频 1.mp4", b"video", "video/mp4")
    provider_url = public_store.provider_url("项目 A/视频 1.mp4")
    assert provider_url is not None
    assert provider_url.startswith(
        "https://objects.example/provider-media/%E9%A1%B9%E7%9B%AE%20A/%E8%A7%86%E9%A2%91%201.mp4"
    )
    assert "expires=1300" in provider_url
    assert "signature=" in provider_url

    with pytest.raises(ValueError, match="HTTPS"):
        ProviderMediaUrlSigner(
            base_url="http://objects.example/media",
            signing_secret="provider-media-test-secret-that-is-long-enough",
        )


def test_local_asset_store_blocks_escape_and_video_download_is_bounded(monkeypatch, tmp_path):
    store = LocalAssetStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="超出"):
        store.put_bytes("../escape.bin", b"x", "application/octet-stream")

    class StreamResponse(FakeResponse):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def iter_bytes(self, _):
            yield b"video-"
            yield b"bytes"

    class StreamClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def stream(self, method, endpoint):
            assert method == "GET"
            assert endpoint == "https://media.example/result.mp4"
            return StreamResponse(headers={"content-type": "video/mp4", "content-length": "11"})

    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: StreamClient())
    result = download_generated_video(
        AiVideoTask(
            "task-1",
            "succeeded",
            "provider",
            "model",
            video_url="https://media.example/result.mp4",
        ),
        asset_store=store,
        storage_key="tasks/task-1/clip.mp4",
        max_bytes=12,
    )
    assert result.size == 11
    assert store.read_bytes(result.key) == b"video-bytes"

    with pytest.raises(ProviderCallError, match="Mock"):
        download_generated_video(
            "mock://video/1",
            asset_store=store,
            storage_key="unused.mp4",
        )


def test_document_and_official_jsonl_ingestion_preserve_source_metadata(tmp_path):
    document = tmp_path / "brand.md"
    document.write_text("# 品牌资料\n\n第一段。\n\n第二段。", encoding="utf-8")
    result = ingest_document_file(
        KnowledgeIngestRequest(
            source_path=document,
            owner_id="user-a",
            category="brand",
            source_uri="https://example.com/brand",
        )
    )
    assert extract_document_text(document).startswith("# 品牌资料")
    assert result.chunks
    assert result.chunks[0].source_uri == "https://example.com/brand"

    docx_path = tmp_path / "product.docx"
    docx = Document()
    docx.add_paragraph("产品资料正文")
    table = docx.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "产品"
    table.cell(0, 1).text = "卖点"
    docx.save(docx_path)
    assert "产品资料正文" in extract_document_text(docx_path)
    assert "产品 卖点" in extract_document_text(docx_path)

    fixture = ROOT / "tests" / "fixtures" / "content_knowledge" / "sample_brand"
    imported = load_official_jsonl_bundle(fixture)
    categories = discover_official_jsonl_categories(fixture)
    assert imported
    assert {item.id for item in categories} >= {"enterprise", "culture", "product"}
    assert any(item.metadata.get("source_url") for item in imported)


def test_ffmpeg_composer_applies_two_shots_fade_subtitle_voice_and_logo(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    logo = tmp_path / "logo.png"
    Image.new("RGB", (160, 96), "red").save(first)
    Image.new("RGB", (160, 96), "blue").save(second)
    Image.new("RGBA", (40, 16), (255, 255, 255, 180)).save(logo)
    speech = MockSpeechSynthesisPort().synthesize(
        SpeechSynthesisRequest("voice", audio_format="wav")
    )
    voice = tmp_path / "voice.wav"
    voice.write_bytes(speech.audio)
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text(
        render_srt([SubtitleSegment(1, "subtitle", 0, 0.5, "estimated")]),
        encoding="utf-8",
    )
    output = tmp_path / "output.mp4"
    composer = FfmpegVideoComposerPort(timeout_seconds=60)

    result = composer.compose(
        VideoCompositionRequest(
            clips=(
                VideoClip(first, 0.6),
                VideoClip(second, 0.6, "fade", 0.15),
            ),
            output_path=output,
            width=160,
            height=96,
            voice_path=voice,
            subtitle_path=subtitle,
            background_music_path=voice,
            logo_path=logo,
            logo_width=40,
            intro_path=first,
            intro_duration_seconds=0.2,
            outro_path=second,
            outro_duration_seconds=0.2,
        )
    )

    assert output.stat().st_size > 0
    assert result.clip_count == 4
    assert "fade" in result.transitions_applied
    assert result.subtitle_burned and result.voice_applied and result.logo_applied
    assert result.background_music_applied

    recomposed = tmp_path / "recomposed.mp4"
    video_result = composer.compose(
        VideoCompositionRequest(
            clips=(
                VideoClip(output, 0.5, is_image=False),
                VideoClip(output, 0.5, "fade", 0.1, is_image=False),
            ),
            output_path=recomposed,
            width=160,
            height=96,
        )
    )
    assert recomposed.stat().st_size > 0
    assert video_result.transitions_applied == ("fade",)


def test_ffmpeg_composer_builds_safe_light_edit_filter_graph(tmp_path, monkeypatch):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    logo = tmp_path / "logo.png"
    music = tmp_path / "music.wav"
    subtitle = tmp_path / "subtitle.srt"
    output = tmp_path / "output.mp4"
    Image.new("RGB", (96, 160), "red").save(first)
    Image.new("RGB", (96, 160), "blue").save(second)
    Image.new("RGBA", (40, 16), (255, 255, 255, 180)).save(logo)
    music.write_bytes(
        MockSpeechSynthesisPort()
        .synthesize(SpeechSynthesisRequest("music", audio_format="wav"))
        .audio
    )
    subtitle.write_text(
        render_srt([SubtitleSegment(1, "安全字幕", 0, 0.5, "estimated")]),
        encoding="utf-8",
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"fake-mp4")
        return media_adapters.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(media_adapters.subprocess, "run", fake_run)
    composer = FfmpegVideoComposerPort(timeout_seconds=60)
    composer.compose(
        VideoCompositionRequest(
            clips=(
                VideoClip(first, 0.6, fit_mode="cover"),
                VideoClip(second, 0.6, "fade", 0.2, fit_mode="cover"),
            ),
            output_path=output,
            width=160,
            height=96,
            subtitle_path=subtitle,
            subtitle_style={
                "font_size": 42,
                "primary_color": "#112233",
                "outline_color": "#000000",
                "outline_width": 3,
                "alignment": 2,
                "margin_v": 28,
                "bold": True,
            },
            background_music_path=music,
            background_music_volume=0.35,
            logo_path=logo,
            logo_width=40,
            logo_position="bottom_left",
        )
    )

    command = captured["command"]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "force_original_aspect_ratio=increase" in filter_graph
    assert "crop=160:96" in filter_graph
    assert "fade=t=in:st=0:d=0.200" in filter_graph
    assert "force_style='FontSize=42" in filter_graph
    assert "PrimaryColour=&H00332211" in filter_graph
    assert "overlay=24:H-h-24" in filter_graph
    assert "volume=0.350" in filter_graph
