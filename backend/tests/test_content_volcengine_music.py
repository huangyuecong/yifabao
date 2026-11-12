import hashlib
import io
import json
import math
import os
import struct
import subprocess
import wave
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from content_service import volcengine_music
from content_service.config import load_settings
from content_service.llm_adapters import ProviderCallError
from content_service.providers import MusicGenerationRequest
from content_service.volcengine_music import VolcengineMusicGenerationPort, signed_music_headers


def _wav(duration=0.25, *, tone=False):
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        samples = int(8000 * duration)
        stream.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(index * math.tau * 440 / 8000)))
                for index in range(samples)
            )
            if tone
            else b"\0\0" * samples
        )
    return target.getvalue()


def _provider(**kwargs):
    return VolcengineMusicGenerationPort(access_key="test-ak", secret_key="test-sk", **kwargs)


def _mock_client(monkeypatch, responses):
    calls = []
    queue = iter(responses)
    original_client = httpx.Client

    def handle(request):
        calls.append(request)
        return next(queue)

    monkeypatch.setattr(volcengine_music.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        volcengine_music.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls


def _success(audio=None, url="https://music.example.volces.com/result.wav"):
    return [
        httpx.Response(200, json={"Code": 0, "Result": {"TaskID": "music-job"}}),
        httpx.Response(200, json={"Result": {"Status": 1}}),
        httpx.Response(
            200,
            json={
                "Result": {
                    "Status": 2,
                    "SongDetail": {
                        "AudioUrl": url,
                        "Duration": 30,
                    },
                }
            },
        ),
        httpx.Response(200, content=audio or _wav(), headers={"content-type": "audio/wav"}),
    ]


def test_default_music_is_real_unconfigured_not_mock_and_secrets_are_separate(
    monkeypatch, tmp_path
):
    for key in list(os.environ):
        if key.startswith("CONTENT_MUSIC_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONTENT_ARK_API_KEY", "ark-is-not-music-aksk")
    settings = load_settings()
    assert settings.music_provider == "volcengine"
    assert settings.music_access_key is settings.music_secret_key is None
    assert settings.music_model == "v5.0"
    assert (settings.music_min_duration_seconds, settings.music_max_duration_seconds) == (30, 120)
    monkeypatch.setenv("CONTENT_MUSIC_ACCESS_KEY", "music-ak-private")
    monkeypatch.setenv("CONTENT_MUSIC_SECRET_KEY", "music-sk-private")
    settings = load_settings()
    assert settings.music_access_key == "music-ak-private"
    assert settings.music_secret_key == "music-sk-private"
    assert "music-ak-private" not in repr(settings)
    assert "music-sk-private" not in repr(settings)
    monkeypatch.setenv("CONTENT_MUSIC_PROVIDER", "mock")
    assert load_settings().music_provider == "mock"


@pytest.mark.parametrize("billing,action", [("prepaid", "GenBGM"), ("postpaid", "GenBGMForTime")])
def test_volcengine_bgm_submits_polls_downloads_real_bytes_without_leaking_auth(
    monkeypatch, billing, action
):
    calls = _mock_client(monkeypatch, _success())
    result = _provider(billing=billing).generate(
        MusicGenerationRequest("温暖钢琴，无人声", 30, "wav")
    )
    assert [call.url.params["Action"] for call in calls[:3]] == [action, "QuerySong", "QuerySong"]
    assert json.loads(calls[0].content) == {
        "Text": "温暖钢琴，无人声",
        "Version": "v5.0",
        "Duration": 30,
        "EnableInputRewrite": False,
    }
    assert calls[0].headers["x-content-sha256"] == hashlib.sha256(calls[0].content).hexdigest()
    assert "/cn-beijing/imagination/request" in calls[0].headers["authorization"]
    assert "test-sk" not in calls[0].headers["authorization"]
    assert "authorization" not in calls[-1].headers
    assert "x-date" not in calls[-1].headers
    assert result.audio == _wav()
    assert result.duration_seconds == 0.25  # Actual decoded duration, never claimed target.
    assert result.requested_duration_seconds == 30
    assert result.metadata["provider_task_id"] == "music-job"
    assert "AudioUrl" not in json.dumps(result.metadata)
    assert _provider().capabilities["sound_effects"] is False


def test_music_signing_is_deterministic_and_bound_to_action_and_body():
    now = datetime(2025, 12, 1, 10, 38, 33, tzinfo=UTC)
    body = b'{"Text":"test","Duration":30}'
    headers = signed_music_headers("ak", "sk", "GenBGM", body, now=now)
    assert headers["x-date"] == "20251201T103833Z"
    assert headers == signed_music_headers("ak", "sk", "GenBGM", body, now=now)
    assert (
        headers["Authorization"]
        != signed_music_headers("ak", "sk", "QuerySong", body, now=now)["Authorization"]
    )
    assert (
        headers["Authorization"]
        != signed_music_headers("ak", "sk", "GenBGM", b"{}", now=now)["Authorization"]
    )


@pytest.mark.parametrize("duration", [0, -1, 121, 300, 0.5, 30.5, float("nan"), float("inf"), True])
def test_music_rejects_unsupported_durations_without_silent_clamping(duration):
    with pytest.raises(ValueError, match="时长"):
        _provider().generate(MusicGenerationRequest("test", duration))


def test_missing_credentials_is_clear_and_every_attempt_remains_retryable():
    provider = VolcengineMusicGenerationPort()
    for _ in range(2):
        with pytest.raises(ProviderCallError, match="方舟密钥不能") as caught:
            provider.generate(MusicGenerationRequest("钢琴音乐", 30))
        assert caught.value.code == "music_credentials_missing"
        assert caught.value.retryable is True


@pytest.mark.parametrize(
    "code,status,message",
    [
        ("SignatureDoesNotMatch", 403, "密钥校验失败"),
        ("AccessDenied", 403, "尚未开通"),
        ("QuotaExceeded", 400, "额度不足"),
        ("TooManyRequests", 429, "请求较多"),
        ("InternalError", 500, "暂时无法"),
        ("SensitiveContent", 400, "审核"),
    ],
)
def test_supplier_errors_are_clear_chinese_and_do_not_expose_raw_response(
    monkeypatch, code, status, message
):
    calls = _mock_client(
        monkeypatch,
        [
            httpx.Response(
                status,
                json={
                    "ResponseMetadata": {
                        "Error": {
                            "Code": code,
                            "Message": "test-sk https://secret.example/?signature=private",
                        }
                    }
                },
            )
        ],
    )
    with pytest.raises(ProviderCallError, match=message) as caught:
        _provider().generate(MusicGenerationRequest("test", 30))
    assert "test-sk" not in str(caught.value)
    assert "https://" not in str(caught.value)
    assert len(calls) == 1  # Never automatically resubmit a potentially paid request.


@pytest.mark.parametrize(
    "url",
    ["http://music.volces.com/a.mp3", "https://127.0.0.1/a.mp3", "https://evil.example/a.mp3"],
)
def test_music_download_is_https_allowlisted_and_never_hits_untrusted_host(monkeypatch, url):
    calls = _mock_client(monkeypatch, _success(url=url))
    with pytest.raises(ProviderCallError, match="安全检查"):
        _provider().generate(MusicGenerationRequest("test", 30))
    assert len(calls) == 3


def test_music_download_does_not_follow_redirect(monkeypatch):
    responses = _success()
    responses[-1] = httpx.Response(302, headers={"location": "https://evil.example"})
    calls = _mock_client(monkeypatch, responses)
    with pytest.raises(ProviderCallError, match="跳转"):
        _provider().generate(MusicGenerationRequest("test", 30))
    assert len(calls) == 4


def test_music_rejects_non_audio(monkeypatch):
    _mock_client(monkeypatch, _success(audio=b"not an audio file"))
    with pytest.raises(ProviderCallError, match="不是有效"):
        _provider().generate(MusicGenerationRequest("test", 30))


def test_music_download_enforces_size_limit(monkeypatch):
    calls = _mock_client(monkeypatch, _success())
    with pytest.raises(ProviderCallError, match="超过允许大小"):
        _provider(max_bytes=1024).generate(MusicGenerationRequest("test", 30))
    assert len(calls) == 4


def test_music_failed_poll_stops_and_does_not_download_or_resubmit(monkeypatch):
    responses = _success()[:1] + [
        httpx.Response(
            200,
            json={
                "Result": {
                    "Status": 3,
                    "FailureReason": {"Code": "SensitiveContent", "Msg": "private prompt"},
                }
            },
        )
    ]
    calls = _mock_client(monkeypatch, responses)
    with pytest.raises(ProviderCallError, match="调整描述") as caught:
        _provider().generate(MusicGenerationRequest("test", 30))
    assert "private prompt" not in str(caught.value)
    assert len(calls) == 2


def test_music_poll_timeout_never_resubmits(monkeypatch):
    calls = _mock_client(monkeypatch, _success()[:1])
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(volcengine_music.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ProviderCallError, match="等待超时") as caught:
        _provider(timeout_seconds=1).generate(MusicGenerationRequest("test", 30))
    assert caught.value.code == "music_timeout"
    assert caught.value.retryable
    assert len(calls) == 1


def test_music_wav_mp3_conversion_uses_real_ffmpeg_not_extension_renaming():
    provider = _provider()
    mp3 = provider._convert(_wav(), "mp3")
    assert not mp3.startswith(b"RIFF")
    converted = provider._convert(mp3, "wav")
    with wave.open(io.BytesIO(converted)) as stream:
        assert 0.2 <= stream.getnframes() / stream.getframerate() <= 0.4


@pytest.mark.parametrize("duration", [1, 5, 15, 29, 30])
@pytest.mark.parametrize("audio_format", ["wav", "mp3"])
def test_short_music_submits_real_minimum_then_trims_samples_and_fades_out(
    monkeypatch, duration, audio_format
):
    provider = _provider()
    source = _wav(30, tone=True)
    calls = _mock_client(monkeypatch, _success(audio=source))
    result = provider.generate(MusicGenerationRequest("无人声钢琴", duration, audio_format))
    assert json.loads(calls[0].content)["Duration"] == 30
    assert result.requested_duration_seconds == duration
    assert result.duration_seconds == pytest.approx(duration, abs=0.002)
    assert result.metadata["provider_requested_duration_seconds"] == 30
    assert result.metadata["provider_reported_duration_seconds"] == 30
    assert result.metadata["source_duration_seconds"] == 30
    assert result.metadata["trim_applied"] is (duration < 30)
    assert result.metadata["fade_out_seconds"] == (0.2 if duration < 30 else 0)
    decoded = provider._convert(result.audio, "wav")
    with wave.open(io.BytesIO(decoded)) as stream:
        assert stream.getnframes() / stream.getframerate() == pytest.approx(duration, abs=0.002)
        samples = struct.unpack(f"<{stream.getnframes()}h", stream.readframes(stream.getnframes()))
    assert max(abs(value) for value in samples[:800]) > 9000  # Source music, not silence.
    if duration < 30:
        assert max(abs(value) for value in samples[-80:]) < 1000
        assert result.audio != source
    elif audio_format == "wav":
        assert result.audio == source  # No unrequested trimming or fade at native 30s.
    assert provider.capabilities["duration_seconds"] == {
        "minimum": 1,
        "maximum": 120,
        "provider_minimum": 30,
        "control": "provider-duration-with-trim",
    }
    assert len(calls) == 4


@pytest.mark.parametrize("audio_format", ["wav", "mp3"])
def test_short_music_accepts_real_mp3_supplier_audio(monkeypatch, audio_format):
    provider = _provider()
    mp3 = provider._convert(_wav(30, tone=True), "mp3")
    _mock_client(monkeypatch, _success(audio=mp3))
    result = provider.generate(MusicGenerationRequest("无人声", 5, audio_format))
    assert result.duration_seconds == pytest.approx(5, abs=0.002)
    assert provider._decoded_duration(result.audio) == pytest.approx(5, abs=0.002)


def test_short_music_respects_configured_upstream_minimum(monkeypatch):
    calls = _mock_client(monkeypatch, _success(audio=_wav(45, tone=True)))
    result = _provider(minimum_duration_seconds=45, maximum_duration_seconds=90).generate(
        MusicGenerationRequest("无人声", 5, "wav"),
    )
    assert json.loads(calls[0].content)["Duration"] == 45
    assert result.duration_seconds == 5
    assert result.metadata["provider_requested_duration_seconds"] == 45
    assert result.metadata["source_duration_seconds"] == 45


@pytest.mark.parametrize("audio_format", ["wav", "mp3"])
def test_short_supplier_result_fails_without_silence_padding(monkeypatch, audio_format):
    provider = _provider()
    source = provider._convert(_wav(0.25, tone=True), audio_format)
    calls = _mock_client(monkeypatch, _success(audio=source))
    with pytest.raises(ProviderCallError, match="不足所选的 5 秒") as caught:
        provider.generate(MusicGenerationRequest("无人声", 5, audio_format))
    assert caught.value.code == "music_result_too_short"
    assert caught.value.retryable
    assert len(calls) == 4


def test_short_music_trim_failure_is_clear_and_does_not_return_fake_audio(monkeypatch):
    _mock_client(monkeypatch, _success(audio=_wav(30, tone=True)))

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"private local path")

    monkeypatch.setattr(volcengine_music.subprocess, "run", fail)
    with pytest.raises(ProviderCallError, match="短配乐裁切失败") as caught:
        _provider().generate(MusicGenerationRequest("无人声", 5, "wav"))
    assert "ffmpeg" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_short_music_rejects_corrupt_mp3_instead_of_trusting_reported_duration(monkeypatch):
    _mock_client(monkeypatch, _success(audio=b"ID3" + b"broken audio" * 100))
    with pytest.raises(ProviderCallError, match="音频格式转换失败"):
        _provider().generate(MusicGenerationRequest("无人声", 5, "mp3"))


@pytest.mark.parametrize("duration", [1, 30])
@pytest.mark.parametrize("audio_format", ["wav", "mp3"])
def test_music_rejects_truncated_wav_even_when_header_claims_enough_duration(
    monkeypatch, duration, audio_format
):
    # Header declares 30 seconds, but only two seconds of PCM are available.
    # Even a 1s trim must reject corruption instead of reporting a false source duration.
    truncated = _wav(30, tone=True)[: 44 + 8000 * 2 * 2]
    calls = _mock_client(monkeypatch, _success(audio=truncated))
    with pytest.raises(ProviderCallError, match="文件不完整或已损坏") as caught:
        _provider().generate(MusicGenerationRequest("无人声", duration, audio_format))
    assert caught.value.code == "music_audio_invalid"
    assert caught.value.retryable
    assert len(calls) == 4


def test_music_rejects_wav_with_incomplete_pcm_sample():
    malformed = _wav(1, tone=True)[:-1]
    with pytest.raises(ProviderCallError, match="文件不完整或已损坏"):
        _provider()._decoded_duration(malformed)


@pytest.mark.parametrize("mode", ["full", "video"])
def test_shared_music_api_missing_key_does_not_create_fake_assets_and_can_retry(
    content_client, user_headers, mode
):
    from content_service.main import app, get_music_generation_port

    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": mode,
            "name": f"真实音乐-{uuid4().hex[:8]}",
            "topic": "品牌短片",
            "audience": "成年消费者",
        },
    ).json()
    project = content_client.post(
        f"/api/v1/tasks/{task['id']}/video-project", headers=user_headers
    ).json()
    original = app.dependency_overrides.get(get_music_generation_port)
    provider = VolcengineMusicGenerationPort()
    app.dependency_overrides[get_music_generation_port] = lambda: provider
    try:
        for _ in range(2):
            response = content_client.post(
                f"/api/v1/video-projects/{project['id']}/generate-music",
                headers=user_headers,
                json={
                    "expected_revision": project["current_revision"],
                    "prompt": "钢琴，无人声",
                    "duration_seconds": 15,
                },
            )
            assert response.status_code == 502, response.text
            assert "专用访问密钥" in response.text
        updated = content_client.get(
            f"/api/v1/video-projects/{project['id']}", headers=user_headers
        ).json()
        assert updated["assets"] == project["assets"]
        assert updated["current_revision_id"] == project["current_revision_id"]
    finally:
        if original is None:
            app.dependency_overrides.pop(get_music_generation_port, None)
        else:
            app.dependency_overrides[get_music_generation_port] = original


@pytest.mark.parametrize("mode", ["full", "video"])
@pytest.mark.parametrize("duration", [5, 15])
def test_shared_music_api_persists_real_provider_audio_without_changing_timeline(
    content_client, user_headers, monkeypatch, mode, duration
):
    from content_service.db import SessionLocal
    from content_service.main import app, get_music_generation_port
    from content_service.models import Artifact

    task = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": mode,
            "name": f"音乐落盘-{uuid4().hex[:8]}",
            "topic": "品牌短片",
            "audience": "成年消费者",
        },
    ).json()
    project = content_client.post(
        f"/api/v1/tasks/{task['id']}/video-project", headers=user_headers
    ).json()
    original = app.dependency_overrides.get(get_music_generation_port)
    provider = _provider()
    app.dependency_overrides[get_music_generation_port] = lambda: provider
    source = _wav(30, tone=True)
    calls = _mock_client(monkeypatch, _success(audio=source))
    try:
        response = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-music",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "温暖钢琴，无人声",
                "duration_seconds": duration,
                "audio_format": "wav",
                "label": "背景音乐",
            },
        )
        assert response.status_code == 200, response.text
        asset = response.json()
        assert asset["role"] == "music"
        assert asset["metadata"]["provider"] == "volcengine-bgm"
        assert asset["metadata"]["requested_duration_seconds"] == duration
        assert asset["metadata"]["duration_seconds"] == duration
        with SessionLocal() as session:
            artifact = session.get(Artifact, asset["artifact_id"])
            saved = json.loads(artifact.content_json)
            assert saved["duration_seconds"] == duration
            assert saved["metadata"]["provider_requested_duration_seconds"] == 30
            assert saved["metadata"]["source_duration_seconds"] == 30
            assert saved["metadata"]["trim_applied"] is (duration < 30)
            assert "AudioUrl" not in artifact.content_json
        downloaded = content_client.get(asset["original_url"], headers=user_headers)
        assert downloaded.status_code == 200
        assert provider._decoded_duration(downloaded.content) == duration
        if duration == 30:
            assert downloaded.content == source
        updated = content_client.get(
            f"/api/v1/video-projects/{project['id']}", headers=user_headers
        ).json()
        assert updated["current_revision_id"] == project["current_revision_id"]
        assert any(item["id"] == asset["id"] for item in updated["assets"])
        assert len(calls) == 4
    finally:
        if original is None:
            app.dependency_overrides.pop(get_music_generation_port, None)
        else:
            app.dependency_overrides[get_music_generation_port] = original
