import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CONTENT_SRC = ROOT / "services" / "content_service" / "src"
if str(CONTENT_SRC) not in sys.path:
    sys.path.insert(0, str(CONTENT_SRC))

from content_service import media_adapters
from content_service.config import load_settings
from content_service.llm_adapters import ProviderCallError
from content_service.media_adapters import (
    MiniMaxMusicGenerationPort,
    MockMusicGenerationPort,
    MockSpeechSynthesisPort,
)
from content_service.providers import (
    MusicGenerationRequest,
    SpeechSynthesisRequest,
)
from content_service.timeline_media import (
    FfmpegTimelineRenderer,
    TimelineAudioTrack,
    TimelineClip,
    TimelineDucking,
    TimelineLogo,
    TimelineRenderRequest,
    TimelineSubtitle,
)


class StreamResponse:
    def __init__(self, content: bytes, *, content_type: str = "application/json"):
        self.content = content
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(content)),
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=1024 * 1024):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]


class StreamClient:
    def __init__(self, post_payload: dict, *, download=None):
        self.post_payload = post_payload
        self.download = download
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def stream(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if method == "POST":
            return StreamResponse(json.dumps(self.post_payload).encode("utf-8"))
        if self.download is None:
            raise AssertionError("unexpected download")
        content, media_type = self.download
        return StreamResponse(content, content_type=media_type)


def _clear_music_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CONTENT_MUSIC_"):
            monkeypatch.delenv(name, raising=False)


def _wav_bytes(duration=0.25):
    return (
        MockMusicGenerationPort(minimum_duration_seconds=0.1)
        .generate(MusicGenerationRequest("quiet brand cue", duration, audio_format="wav"))
        .audio
    )


def test_music_settings_require_real_provider_and_key_selects_minimax(monkeypatch, tmp_path):
    _clear_music_env(monkeypatch)
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path / "mock"))
    settings = load_settings()
    assert settings.music_provider == "volcengine"
    assert settings.music_api_key is None
    assert settings.music_response_format == "hex"

    monkeypatch.setenv("CONTENT_MUSIC_API_KEY", "server-only-music-key")
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path / "external"))
    settings = load_settings()
    assert settings.music_provider == "minimax"
    assert settings.music_base_url == "https://api.minimaxi.com"
    assert settings.music_model == "music-2.6"
    assert settings.music_api_key == "server-only-music-key"
    assert "server-only-music-key" not in repr(settings)


def test_mock_music_returns_persistable_wav_bytes_and_exact_duration():
    provider = MockMusicGenerationPort(
        minimum_duration_seconds=0.1,
        maximum_duration_seconds=10,
    )
    result = provider.generate(
        MusicGenerationRequest(
            "warm ambient piano without vocals",
            0.25,
            audio_format="wav",
            sample_rate=16000,
        )
    )

    assert result.audio.startswith(b"RIFF")
    assert result.audio[8:12] == b"WAVE"
    assert result.media_type == "audio/wav"
    assert result.duration_seconds == pytest.approx(0.25)
    assert result.requested_duration_seconds == 0.25
    assert provider.capabilities["instrumental_only"] is True
    assert provider.capabilities["returns_bytes"] is True


def test_minimax_music_hex_contract_is_instrumental_and_does_not_invent_duration(monkeypatch):
    audio = _wav_bytes()
    client = StreamClient(
        {
            "data": {"audio": audio.hex(), "status": 2},
            "trace_id": "trace-safe",
            "extra_info": {
                "music_duration": 250,
                "music_sample_rate": 44100,
                "music_channel": 2,
                "bitrate": 256000,
                "music_size": len(audio),
            },
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = MiniMaxMusicGenerationPort(
        provider_id="minimax-test",
        base_url="https://api.minimaxi.com",
        api_key="server-key",
        timeout_seconds=20,
    )

    result = provider.generate(
        MusicGenerationRequest("calm enterprise background", 12, audio_format="wav")
    )

    _, endpoint, kwargs = client.calls[0]
    assert endpoint == "https://api.minimaxi.com/v1/music_generation"
    assert kwargs["headers"]["Authorization"] == "Bearer server-key"
    assert kwargs["json"]["model"] == "music-2.6"
    assert kwargs["json"]["is_instrumental"] is True
    assert kwargs["json"]["output_format"] == "hex"
    assert "duration" not in kwargs["json"]
    assert result.audio == audio
    assert result.duration_seconds == pytest.approx(0.25)
    assert result.requested_duration_seconds == 12
    assert "server-key" not in repr(result)


def test_minimax_music_url_is_downloaded_immediately_with_mime_and_host_checks(monkeypatch):
    audio = b"ID3" + b"\x00" * 64
    temporary_url = "https://media.minimax.example/generated/temporary.mp3?signature=secret"
    client = StreamClient(
        {
            "data": {"audio": temporary_url, "status": 2},
            "trace_id": "trace-url",
            "extra_info": {"music_duration": 1000},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        },
        download=(audio, "audio/mpeg"),
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = MiniMaxMusicGenerationPort(
        provider_id="minimax-test",
        base_url="https://api.minimaxi.com",
        api_key="server-key",
        allowed_download_hosts=("minimax.example",),
    )

    result = provider.generate(
        MusicGenerationRequest(
            "short logo sting",
            1,
            audio_format="mp3",
            response_format="url",
        )
    )

    assert result.audio == audio
    assert [call[0] for call in client.calls] == ["POST", "GET"]
    assert temporary_url not in repr(result.metadata)
    assert result.metadata["supplier_response_format"] == "url"


def test_minimax_music_rejects_private_url_and_invalid_audio(monkeypatch):
    client = StreamClient(
        {
            "data": {"audio": "https://127.0.0.1/internal.wav", "status": 2},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        },
        download=(b"not-a-wave", "audio/wav"),
    )
    monkeypatch.setattr(media_adapters.httpx, "Client", lambda **_: client)
    provider = MiniMaxMusicGenerationPort(
        provider_id="minimax-test",
        base_url="https://api.minimaxi.com",
        api_key="server-key",
    )

    with pytest.raises(ProviderCallError, match="非公网 IP"):
        provider.generate(
            MusicGenerationRequest(
                "unsafe url",
                1,
                audio_format="wav",
                response_format="url",
            )
        )


def test_timeline_command_has_true_xfade_trim_split_multitrack_ducking_and_overlays(tmp_path):
    video = tmp_path / "source.mp4"
    voice = tmp_path / "voice.wav"
    music = tmp_path / "music.wav"
    sfx = tmp_path / "sfx.wav"
    logo = tmp_path / "logo.png"
    subtitle = tmp_path / "caption.srt"
    for path in (video, voice, music, sfx):
        path.write_bytes(b"placeholder")
    logo.write_bytes(b"placeholder")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,500\n字幕\n", encoding="utf-8")
    renderer = FfmpegTimelineRenderer(
        allowed_input_roots=(tmp_path,),
        allowed_output_roots=(tmp_path,),
    )
    assert "speed" in renderer.capabilities["audio_controls"]
    request = TimelineRenderRequest(
        clips=(
            TimelineClip(
                "same-video",
                video,
                source_in_seconds=1,
                source_out_seconds=3,
                source_audio="keep",
                source_audio_volume=0.6,
                has_source_audio=True,
            ),
            TimelineClip(
                "same-video",
                video,
                source_in_seconds=4,
                source_out_seconds=6,
                transition="dissolve",
                transition_duration_seconds=0.25,
                source_audio="mute",
                has_source_audio=True,
            ),
        ),
        output_path=tmp_path / "timeline.mp4",
        width=1280,
        height=720,
        audio_tracks=(
            TimelineAudioTrack(
                "voice-a",
                voice,
                "voice",
                offset_seconds=0.5,
                duration_seconds=1.5,
                volume=1.1,
                fade_in_seconds=0.1,
                fade_out_seconds=0.2,
            ),
            TimelineAudioTrack(
                "music-a",
                music,
                "music",
                offset_seconds=0,
                duration_seconds=3.75,
                volume=0.25,
                fade_in_seconds=0.2,
                fade_out_seconds=0.3,
                loop=True,
            ),
            TimelineAudioTrack(
                "sfx-a",
                sfx,
                "sfx",
                offset_seconds=1.25,
                source_in_seconds=0.1,
                source_out_seconds=0.5,
                duration_seconds=0.4,
                volume=0.8,
            ),
        ),
        logos=(
            TimelineLogo("logo-a", logo, 0.4, 2.2, 120, "bottom_left", 0.8),
            TimelineLogo(
                "logo-percent",
                logo,
                0.5,
                2.0,
                80,
                "center",
                0.5,
                x_percent=25,
                y_percent=75,
            ),
        ),
        subtitles=(TimelineSubtitle("subtitle-a", subtitle, {"font_size": 40}),),
        ducking=TimelineDucking(enabled=True, target_kinds=("music",)),
    )

    command = renderer.build_command(request)
    graph = command[command.index("-filter_complex") + 1]

    assert "xfade=transition=dissolve:duration=0.25:offset=1.75" in graph
    assert "trim=start=1:duration=2" in graph
    assert "trim=start=4:duration=2" in graph
    assert graph.count("asource") >= 1
    assert "volume=0.6000" in graph
    assert "adelay=500:all=1" in graph
    assert "afade=t=in:st=0:d=0.1" in graph
    assert "sidechaincompress=" in graph
    assert "overlay=24:H-h-24:eof_action=repeat:enable='between(t,0.4,2.2)'" in graph
    assert (
        "overlay=main_w*0.250000-overlay_w/2:main_h*0.750000-overlay_h/2:"
        "eof_action=repeat:enable='between(t,0.5,2)'"
    ) in graph
    assert "subtitles=filename=" in graph
    assert "-stream_loop" in command
    assert command[-1] == str((tmp_path / "timeline.mp4").resolve())


def test_timeline_audio_speed_derives_output_duration_from_source_range(tmp_path):
    video = tmp_path / "source.mp4"
    voice = tmp_path / "voice.wav"
    video.write_bytes(b"placeholder")
    voice.write_bytes(b"placeholder")
    renderer = FfmpegTimelineRenderer(
        allowed_input_roots=(tmp_path,),
        allowed_output_roots=(tmp_path,),
    )
    request = TimelineRenderRequest(
        clips=(
            TimelineClip(
                "video-a",
                video,
                duration_seconds=3,
                source_audio="mute",
            ),
        ),
        output_path=tmp_path / "speed-derived.mp4",
        width=320,
        height=180,
        audio_tracks=(
            TimelineAudioTrack(
                "voice-a",
                voice,
                "voice",
                source_in_seconds=0.5,
                source_out_seconds=2.5,
                speed=2,
            ),
        ),
    )

    command = renderer.build_command(request)
    graph = command[command.index("-filter_complex") + 1]
    assert "atrim=start=0.5:duration=2" in graph
    assert "atempo=2,apad=pad_dur=1,atrim=duration=1" in graph


def test_timeline_rejects_non_allowlisted_paths_and_filter_like_transition(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    video = outside / "source.mp4"
    video.write_bytes(b"placeholder")
    renderer = FfmpegTimelineRenderer(
        allowed_input_roots=(allowed,),
        allowed_output_roots=(allowed,),
    )
    request = TimelineRenderRequest(
        clips=(
            TimelineClip(
                "asset-a",
                video,
                duration_seconds=1,
                source_audio="mute",
                transition="fade;movie=/etc/passwd",
            ),
        ),
        output_path=allowed / "output.mp4",
        width=320,
        height=180,
    )

    with pytest.raises(ValueError, match="对象存储目录"):
        renderer.build_command(request)


def test_ffmpeg_timeline_render_integration_when_ffmpeg_is_available(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    logo = tmp_path / "logo.png"
    Image.new("RGB", (160, 96), "#b51f2e").save(first)
    Image.new("RGB", (160, 96), "#176b5f").save(second)
    Image.new("RGBA", (40, 16), (255, 255, 255, 190)).save(logo)
    audio = (
        MockSpeechSynthesisPort()
        .synthesize(SpeechSynthesisRequest("timeline audio", audio_format="wav"))
        .audio
    )
    voice = tmp_path / "voice.wav"
    music = tmp_path / "music.wav"
    voice.write_bytes(audio)
    music.write_bytes(audio)
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:00,050 --> 00:00:00,700\nTimeline\n", encoding="utf-8")

    try:
        renderer = FfmpegTimelineRenderer(
            timeout_seconds=60,
            allowed_input_roots=(tmp_path,),
            allowed_output_roots=(tmp_path,),
        )
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")

    output = tmp_path / "rendered.mp4"
    result = renderer.render(
        TimelineRenderRequest(
            clips=(
                TimelineClip("image-a", first, "image", duration_seconds=0.6),
                TimelineClip(
                    "image-b",
                    second,
                    "image",
                    duration_seconds=0.6,
                    transition="fade",
                    transition_duration_seconds=0.2,
                ),
            ),
            output_path=output,
            width=160,
            height=96,
            fps=20,
            audio_tracks=(
                TimelineAudioTrack(
                    "voice-a",
                    voice,
                    "voice",
                    offset_seconds=0.1,
                    duration_seconds=0.5,
                    speed=2,
                    fade_in_seconds=0.05,
                    fade_out_seconds=0.05,
                ),
                TimelineAudioTrack(
                    "music-a",
                    music,
                    "music",
                    duration_seconds=1,
                    volume=0.15,
                    loop=True,
                ),
            ),
            logos=(TimelineLogo("logo-a", logo, 0.2, 0.8, 40),),
            subtitles=(TimelineSubtitle("subtitle-a", subtitle),),
            ducking=TimelineDucking(enabled=True),
        )
    )

    assert output.stat().st_size > 0
    assert result.duration_seconds == pytest.approx(1)
    assert result.transitions_applied == ("fade",)
    assert result.ducking_applied is True
    assert result.logos_applied == 1
    assert result.subtitles_burned == 1
