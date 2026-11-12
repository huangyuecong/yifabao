from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from content_service import media_ingest
from content_service.media_ingest import (
    MediaIngestPolicy,
    MediaValidationError,
    generate_derivatives,
    probe_media,
    read_upload_head,
    safe_filename,
    validate_upload_header,
)


def test_upload_header_uses_signature_instead_of_trusting_mime() -> None:
    kind, media_type = validate_upload_header(
        filename="海报.png",
        declared_media_type="application/octet-stream",
        head=b"\x89PNG\r\n\x1a\n" + b"payload",
        size_bytes=32,
    )
    assert kind == "image"
    assert media_type == "image/png"

    with pytest.raises(MediaValidationError, match="扩展名"):
        validate_upload_header(
            filename="伪装视频.mp4",
            declared_media_type="video/mp4",
            head=b"\x89PNG\r\n\x1a\n" + b"payload",
            size_bytes=32,
        )


def test_subtitle_validation_requires_utf8_text() -> None:
    assert validate_upload_header(
        filename="captions.vtt",
        declared_media_type="text/vtt",
        head=b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello",
    ) == ("subtitle", "text/vtt")
    with pytest.raises(MediaValidationError, match="UTF-8"):
        validate_upload_header(
            filename="captions.srt",
            declared_media_type="text/plain",
            head=b"1\n00:00:00,000 --> 00:00:01,000\n\x00bad",
        )


def test_upload_size_policy_and_stream_position() -> None:
    stream = io.BytesIO(b"ID3" + b"x" * 64)
    stream.seek(5)
    assert read_upload_head(stream).startswith(b"xx")
    assert stream.tell() == 5

    with pytest.raises(MediaValidationError, match="大小限制"):
        validate_upload_header(
            filename="music.mp3",
            declared_media_type="audio/mpeg",
            head=b"ID3payload",
            size_bytes=100,
            policy=MediaIngestPolicy(
                max_bytes={"image": 10, "video": 10, "audio": 10, "subtitle": 10}
            ),
        )


def test_upload_header_covers_common_signatures() -> None:
    assert validate_upload_header(
        filename="poster.jpg",
        declared_media_type="image/jpeg",
        head=b"\xff\xd8\xff" + b"payload",
        size_bytes=32,
    ) == ("image", "image/jpeg")
    assert validate_upload_header(
        filename="poster.webp",
        declared_media_type="image/webp",
        head=b"RIFF....WEBP",
        size_bytes=32,
    ) == ("image", "image/webp")
    assert validate_upload_header(
        filename="clip.mp4",
        declared_media_type="video/mp4",
        head=b"\x00\x00\x00\x18ftypisom",
        size_bytes=64,
    ) == ("video", "video/mp4")
    assert validate_upload_header(
        filename="music.wav",
        declared_media_type="audio/wav",
        head=b"RIFF....WAVE",
        size_bytes=64,
    ) == ("audio", "audio/wav")
    with pytest.raises(MediaValidationError, match="不能为空"):
        validate_upload_header(
            filename="poster.png",
            declared_media_type="image/png",
            head=b"\x89PNG\r\n\x1a\n",
            size_bytes=0,
        )
    with pytest.raises(MediaValidationError, match="媒体类型不一致"):
        validate_upload_header(
            filename="poster.png",
            declared_media_type="image/gif",
            head=b"\x89PNG\r\n\x1a\n" + b"payload",
            size_bytes=32,
        )


def test_safe_filename_removes_paths_and_control_characters() -> None:
    assert safe_filename("../素材/片头\n最终版.mp4") == "片头_最终版.mp4"
    assert safe_filename("") == "material.bin"


def test_probe_subtitle_and_invalid_ffprobe_payload(tmp_path: Path, monkeypatch) -> None:
    captions = tmp_path / "captions.srt"
    captions.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    probe = probe_media(captions, kind="subtitle", media_type="application/x-subrip")
    assert probe.kind == "subtitle"
    assert probe.size_bytes == captions.stat().st_size

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout="not-json",
            stderr="",
        )

    monkeypatch.setattr(media_ingest.shutil, "which", lambda _name: "ffprobe")
    monkeypatch.setattr(media_ingest, "run_captured_process", fake_run)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom")
    with pytest.raises(MediaValidationError, match="无效结果"):
        probe_media(video, kind="video", media_type="video/mp4")


def test_bundled_ffmpeg_probes_and_derives_without_system_ffprobe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "bundled-source.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x17806f:s=320x180:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    monkeypatch.setattr(media_ingest.shutil, "which", lambda _name: None)

    probe = probe_media(source, kind="video", media_type="video/mp4")
    assert (probe.width, probe.height) == (320, 180)
    assert probe.audio_codec
    outputs = generate_derivatives(source, tmp_path / "bundled-derived", probe=probe)
    assert {item.kind for item in outputs} == {"proxy", "thumbnail", "waveform"}


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)
def test_probe_and_derivatives_for_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    completed = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x17806f:s=640x360:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    probe = probe_media(source, kind="video", media_type="video/mp4")
    assert probe.width == 640
    assert probe.height == 360
    assert probe.duration_seconds is not None and probe.duration_seconds >= 0.9
    assert probe.video_codec
    assert probe.audio_codec

    outputs = generate_derivatives(source, tmp_path / "derived", probe=probe)
    assert {item.kind for item in outputs} == {"proxy", "thumbnail", "waveform"}
    assert all(item.path.exists() and item.path.stat().st_size > 0 for item in outputs)
