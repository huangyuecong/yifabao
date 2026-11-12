"""No paid API calls: real media extraction with mocked Ark responses."""

import io
import json
import os
import subprocess
import sys
import wave

import httpx
import pytest

from content_service import seedance_music
from content_service.config import load_settings
from content_service.llm_adapters import ProviderCallError
from content_service.media_adapters import _ffmpeg_executable
from content_service.providers import MusicGenerationRequest
from content_service.seedance_music import DEFAULT_SEEDANCE_MUSIC_MODEL, SeedanceMusicGenerationPort


def test_seedance_settings_allow_cold_model_import():
    # A fresh process must import ORM models before adapters, as uvicorn does.
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "from content_service.models import Artifact; "
                "from content_service.config import load_settings; "
                "from content_service.seedance_music import DEFAULT_SEEDANCE_MUSIC_MODEL; "
                "assert load_settings().music_model == DEFAULT_SEEDANCE_MUSIC_MODEL"
            ),
        ],
        env={**os.environ, "CONTENT_MUSIC_PROVIDER": "seedance", "CONTENT_MUSIC_MODEL": ""},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def video_bytes(tmp_path_factory):
    directory = tmp_path_factory.mktemp("seedance-music-fixture")
    result = {}
    for audio in (True, False):
        path = directory / f"test-{audio}.mp4"
        command = [
            _ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=5",
        ]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-c:a", "aac"]
        subprocess.run(
            [*command, "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        result[audio] = path.read_bytes()
    return result


def mock_http(monkeypatch, responses):
    calls = []
    queue = iter(responses)
    client = httpx.Client

    def handle(request):
        calls.append(request)
        return next(queue)

    monkeypatch.setattr(seedance_music.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )
    return calls


def success_responses(video, url="https://video.test.volces.com/music.mp4?signature=private"):
    return [
        httpx.Response(200, json={"id": "music-job", "status": "queued"}),
        httpx.Response(
            200, json={"id": "music-job", "status": "succeeded", "content": {"video_url": url}}
        ),
        httpx.Response(200, content=video, headers={"content-type": "video/mp4"}),
    ]


@pytest.mark.parametrize("duration,fmt", [(1, "wav"), (3, "mp3"), (5, "wav")])
def test_seedance_generates_video_then_extracts_real_audio(monkeypatch, video_bytes, duration, fmt):
    calls = mock_http(monkeypatch, success_responses(video_bytes[True]))
    result = SeedanceMusicGenerationPort(api_key="ark-private").generate(
        MusicGenerationRequest("喜庆纯器乐", duration, fmt),
    )
    payload = json.loads(calls[0].content)
    assert payload["model"] == DEFAULT_SEEDANCE_MUSIC_MODEL
    assert payload["generate_audio"] is True
    assert payload["duration"] == 5
    assert payload["ratio"] == "16:9" and payload["resolution"] == "480p"
    assert len(payload["content"]) == 1 and payload["content"][0]["type"] == "text"
    assert "喜庆纯器乐" in payload["content"][0]["text"]
    assert "不要人声" in payload["content"][0]["text"]
    assert "Authorization" not in calls[-1].headers
    assert calls[0].headers["Authorization"] == "Bearer ark-private"
    assert calls[0].headers.get("Idempotency-Key")
    assert result.extension == fmt
    assert abs(result.duration_seconds - duration) < 0.05
    assert result.metadata["provider_duration_seconds"] == 5
    assert result.metadata["billing_basis"] == "video-generation"
    assert "signature" not in json.dumps(result.metadata)
    assert result.audio != video_bytes[True]
    if fmt == "wav":
        with wave.open(io.BytesIO(result.audio), "rb") as stream:
            assert abs(stream.getnframes() / stream.getframerate() - duration) < 0.05
    else:
        assert result.audio.startswith(b"ID3")


def test_missing_audio_is_not_reported_as_success(monkeypatch, video_bytes):
    mock_http(monkeypatch, success_responses(video_bytes[False]))
    with pytest.raises(ProviderCallError, match="没有可用音轨"):
        SeedanceMusicGenerationPort(api_key="test").generate(MusicGenerationRequest("钢琴", 5))


def test_seedance_20_fast_uses_selected_model_with_audio(monkeypatch, video_bytes):
    calls = mock_http(monkeypatch, success_responses(video_bytes[True]))
    model = "doubao-seedance-2-0-fast-260128"
    port = SeedanceMusicGenerationPort(api_key="test", model=model, maximum_duration_seconds=15)
    result = port.generate(MusicGenerationRequest("轻松纯器乐", 5, "mp3"))
    payload = json.loads(calls[0].content)
    assert payload["model"] == model and payload["generate_audio"] is True
    assert payload["duration"] == 5 and payload["resolution"] == "480p"
    assert result.model == model and result.duration_seconds == 5
    assert port.capabilities["duration_seconds"]["maximum"] == 15
    assert "2.5" not in port.status.name


@pytest.mark.parametrize("duration", [16, 30])
def test_seedance_20_rejects_unsupported_duration_without_submission(monkeypatch, duration):
    calls = mock_http(monkeypatch, [])
    port = SeedanceMusicGenerationPort(
        api_key="test",
        model="doubao-seedance-2-0-fast-260128",
        maximum_duration_seconds=15,
    )
    with pytest.raises(ValueError, match="1 到 15"):
        port.generate(MusicGenerationRequest("钢琴", duration))
    assert not calls


def test_seedance_20_configuration_limits_do_not_change_video(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CONTENT_MUSIC_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTENT_MUSIC_PROVIDER", "seedance")
    monkeypatch.setenv("CONTENT_MUSIC_MODEL", "doubao-seedance-2-0-fast-260128")
    monkeypatch.setenv("CONTENT_AI_VIDEO_MODEL", "doubao-seedance-2-0-fast-260128")
    settings = load_settings()
    assert settings.music_model == settings.ai_video_model
    assert settings.music_max_duration_seconds == 15
    monkeypatch.setenv("CONTENT_MUSIC_MAX_DURATION_SECONDS", "30")
    with pytest.raises(ValueError, match="5 到 15"):
        load_settings()
    with pytest.raises(ValueError, match="5 到 15"):
        SeedanceMusicGenerationPort(
            model="doubao-seedance-2-0-fast-260128", maximum_duration_seconds=30
        )


def test_seedance_20_permission_error_does_not_name_25(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(403, json={"error": {"code": "AccessDenied"}})])
    port = SeedanceMusicGenerationPort(
        api_key="test",
        model="doubao-seedance-2-0-fast-260128",
        maximum_duration_seconds=15,
    )
    with pytest.raises(ProviderCallError, match="权限") as caught:
        port.generate(MusicGenerationRequest("钢琴", 5))
    assert "2.5" not in str(caught.value)


def test_short_result_is_not_looped_or_padded(monkeypatch, video_bytes):
    mock_http(monkeypatch, success_responses(video_bytes[True]))
    with pytest.raises(ProviderCallError, match="不足所选时长"):
        SeedanceMusicGenerationPort(api_key="test").generate(MusicGenerationRequest("钢琴", 7))


@pytest.mark.parametrize("duration", [0, -1, 31, 120, 2.5, float("nan"), float("inf"), True])
def test_bad_duration_does_not_submit(monkeypatch, duration):
    calls = mock_http(monkeypatch, [])
    with pytest.raises(ValueError, match="时长"):
        SeedanceMusicGenerationPort(api_key="test").generate(
            MusicGenerationRequest("钢琴", duration)
        )
    assert not calls


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, "Unauthorized", "密钥"),
        (403, "AccessDenied", "权限"),
        (404, "NotFound", "权限"),
        (429, "RateLimit", "较多"),
        (400, "InputSensitiveContent", "审核"),
        (500, "InternalError", "暂时"),
    ],
)
def test_provider_failure_is_clear_and_never_automatically_resubmits(
    monkeypatch, status, code, expected
):
    calls = mock_http(
        monkeypatch,
        [
            httpx.Response(
                status,
                json={
                    "error": {
                        "code": code,
                        "message": "https://private.example/secret English-detail",
                    },
                },
            )
        ],
    )
    with pytest.raises(ProviderCallError, match=expected) as caught:
        SeedanceMusicGenerationPort(api_key="test").generate(MusicGenerationRequest("钢琴", 5))
    assert len(calls) == 1
    assert "https://" not in str(caught.value) and "English-detail" not in str(caught.value)


@pytest.mark.parametrize(
    "url", ["http://a.volces.com/v.mp4", "https://127.0.0.1/v.mp4", "https://evil.example/v.mp4"]
)
def test_download_security(monkeypatch, video_bytes, url):
    calls = mock_http(monkeypatch, success_responses(video_bytes[True], url=url))
    with pytest.raises(ProviderCallError):
        SeedanceMusicGenerationPort(api_key="test").generate(MusicGenerationRequest("钢琴", 5))
    assert len(calls) == 2


def test_different_task_result_is_rejected(monkeypatch):
    calls = mock_http(
        monkeypatch,
        [
            httpx.Response(200, json={"id": "music-job", "status": "queued"}),
            httpx.Response(200, json={"id": "old-video-job", "status": "succeeded"}),
        ],
    )
    with pytest.raises(ProviderCallError, match="不匹配"):
        SeedanceMusicGenerationPort(api_key="test").generate(MusicGenerationRequest("钢琴", 5))
    assert len(calls) == 2


def test_music_model_and_credentials_are_isolated_from_video(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CONTENT_MUSIC_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTENT_MUSIC_PROVIDER", "seedance")
    monkeypatch.setenv("CONTENT_AI_VIDEO_PROVIDER", "volcengine_ark")
    monkeypatch.setenv("CONTENT_ARK_API_KEY", "shared-key")
    monkeypatch.setenv("CONTENT_AI_VIDEO_MODEL", "doubao-seedance-2-0-fast-260128")
    settings = load_settings()
    assert settings.music_api_key == "shared-key"
    assert settings.music_access_key is settings.music_secret_key is None
    assert settings.music_model == DEFAULT_SEEDANCE_MUSIC_MODEL
    assert settings.ai_video_model == "doubao-seedance-2-0-fast-260128"
    assert (settings.music_min_duration_seconds, settings.music_max_duration_seconds) == (5, 30)
    assert "shared-key" not in repr(settings)
    monkeypatch.setenv("CONTENT_MUSIC_API_KEY", "separate-music-key")
    assert load_settings().music_api_key == "separate-music-key"
    assert load_settings().ai_video_api_key == "shared-key"


def test_music_dependency_builds_a_separate_adapter(monkeypatch):
    from content_service import provider_dependencies

    monkeypatch.setenv("CONTENT_MUSIC_PROVIDER", "seedance")
    monkeypatch.setenv("CONTENT_ARK_API_KEY", "not-a-live-key")
    config = load_settings()
    monkeypatch.setattr(provider_dependencies, "get_runtime_settings", lambda: config)
    monkeypatch.setattr(provider_dependencies, "track_provider", lambda port: port)
    # Bypass lru_cache so the running app and neighboring tests are untouched.
    music = provider_dependencies.get_music_generation_port.__wrapped__()
    assert isinstance(music, SeedanceMusicGenerationPort)
    assert music.capabilities["duration_seconds"]["maximum"] == 30
    assert music.capabilities["duration_seconds"]["minimum"] == 1
    assert music.video_provider.model == DEFAULT_SEEDANCE_MUSIC_MODEL


def test_missing_key_does_not_submit(monkeypatch):
    calls = mock_http(monkeypatch, [])
    with pytest.raises(ProviderCallError, match="方舟访问密钥"):
        SeedanceMusicGenerationPort().generate(MusicGenerationRequest("钢琴", 5))
    assert not calls


def test_missing_extractor_does_not_submit(monkeypatch):
    calls = mock_http(monkeypatch, [])

    def unavailable(*args):
        raise RuntimeError("no executable")

    monkeypatch.setattr(seedance_music, "_ffmpeg_executable", unavailable)
    with pytest.raises(ProviderCallError, match="尚未提交模型"):
        SeedanceMusicGenerationPort(api_key="test").generate(MusicGenerationRequest("钢琴", 5))
    assert not calls


def test_download_redirect_is_not_followed(monkeypatch, video_bytes):
    responses = success_responses(video_bytes[True])
    responses[-1] = httpx.Response(302, headers={"location": "https://evil.example"})
    calls = mock_http(monkeypatch, responses)
    with pytest.raises(ProviderCallError, match="跳转"):
        SeedanceMusicGenerationPort(api_key="test").generate(MusicGenerationRequest("钢琴", 5))
    assert len(calls) == 3


def test_timeout_never_resubmits_or_cancels(monkeypatch):
    calls = mock_http(
        monkeypatch, [httpx.Response(200, json={"id": "music-job", "status": "queued"})]
    )
    now = [0.0]
    monkeypatch.setattr(seedance_music.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        seedance_music.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    with pytest.raises(ProviderCallError, match="可能重复计费"):
        SeedanceMusicGenerationPort(api_key="test", timeout_seconds=1).generate(
            MusicGenerationRequest("钢琴", 5)
        )
    assert len(calls) == 1
