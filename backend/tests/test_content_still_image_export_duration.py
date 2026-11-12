"""Still images have a display duration, not a finite source-video duration."""

import subprocess
from dataclasses import replace

import pytest
from PIL import Image

from content_service import timeline_media
from content_service.timeline_media import (
    FfmpegTimelineRenderer,
    TimelineClip,
    TimelineRenderRequest,
)


@pytest.fixture
def isolated_renderer(tmp_path, monkeypatch):
    # Unit tests compile/resolve actual production clips without running FFmpeg.
    monkeypatch.setattr(timeline_media, "_ffmpeg_executable", lambda _: "isolated-ffmpeg")
    monkeypatch.setattr(timeline_media, "_ffprobe_executable", lambda _: None)
    return FfmpegTimelineRenderer(
        allowed_input_roots=(tmp_path,),
        allowed_output_roots=(tmp_path,),
    )


@pytest.mark.parametrize(
    "extension,probed_duration,display_duration",
    [
        ("jpg", 0.04, 4),
        ("jpg", 0.04, 0.1),
        ("jpg", 0.04, 600),
        ("png", None, 4),
        ("png", 0.001, 30),
        ("webp", 0.04, 4),
        ("webp", 9, 30),
        ("bmp", 0.04, 4),
    ],
)
def test_still_image_keeps_requested_display_duration_despite_probe_duration(
    isolated_renderer, tmp_path, monkeypatch, extension, probed_duration, display_duration
):
    source = tmp_path / f"reference.{extension}"
    Image.new("RGB", (96, 64), "#176b5f").save(source)
    original = source.read_bytes()
    monkeypatch.setattr(timeline_media, "_probe", lambda *_: (probed_duration, False))

    resolved = isolated_renderer._resolve_clip(
        TimelineClip(
            "still-image",
            source,
            kind="image",
            duration_seconds=display_duration,
            source_audio="mute",
        )
    )

    assert resolved.duration == display_duration
    assert resolved.value.source_in_seconds == 0
    assert resolved.value.source_out_seconds is None
    assert resolved.has_audio is False
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "source_duration,source_in,duration,accepted",
    [
        (5.09, 0, 5, True),
        (5.09, 1, 4, True),
        (5.09, 0, 5.2, False),
        (5.09, 1, 5, False),
        (0.04, 0, 4, False),
    ],
)
def test_real_video_source_range_guard_is_not_relaxed(
    isolated_renderer, tmp_path, monkeypatch, source_duration, source_in, duration, accepted
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"probe-is-mocked-for-this-unit-test")
    monkeypatch.setattr(timeline_media, "_probe", lambda *_: (source_duration, False))
    clip = TimelineClip(
        "video-source",
        source,
        kind="video",
        source_in_seconds=source_in,
        source_out_seconds=source_in + duration,
        duration_seconds=duration,
        source_audio="mute",
    )

    if accepted:
        assert isolated_renderer._resolve_clip(clip).duration == duration
    else:
        with pytest.raises(ValueError, match="镜头裁剪范围超过源素材时长"):
            isolated_renderer._resolve_clip(clip)


@pytest.mark.parametrize("duration", [0, -1])
def test_still_images_continue_to_reject_non_positive_display_duration(
    isolated_renderer, tmp_path, monkeypatch, duration
):
    source = tmp_path / "still.jpg"
    Image.new("RGB", (96, 64), "#176b5f").save(source)
    monkeypatch.setattr(timeline_media, "_probe", lambda *_: (0.04, False))
    with pytest.raises(ValueError, match="镜头时长必须大于 0"):
        isolated_renderer._resolve_clip(
            TimelineClip("still", source, kind="image", duration_seconds=duration)
        )


def test_still_image_nonzero_source_in_remains_invalid(isolated_renderer, tmp_path, monkeypatch):
    source = tmp_path / "still.jpg"
    Image.new("RGB", (96, 64), "#176b5f").save(source)
    monkeypatch.setattr(timeline_media, "_probe", lambda *_: (0.04, False))
    with pytest.raises(ValueError, match="图片镜头不支持非零入点"):
        isolated_renderer._resolve_clip(
            TimelineClip("still", source, kind="image", source_in_seconds=1, duration_seconds=4)
        )


def test_four_second_jpeg_builds_looped_input_without_changing_timeline_or_original(
    isolated_renderer, tmp_path, monkeypatch
):
    source = tmp_path / "still.jpg"
    Image.new("RGB", (96, 64), "#176b5f").save(source)
    original = source.read_bytes()
    monkeypatch.setattr(timeline_media, "_probe", lambda *_: (0.04, False))
    request = TimelineRenderRequest(
        clips=(TimelineClip("still", source, kind="image", duration_seconds=4),),
        output_path=tmp_path / "not-rendered.mp4",
        width=96,
        height=64,
        fps=25,
    )

    command = isolated_renderer.build_command(request)
    position = command.index("-loop")
    assert command[position : position + 6] == ("-loop", "1", "-framerate", "25", "-t", "4")
    graph = command[command.index("-filter_complex") + 1]
    assert "trim=start=0:duration=4" in graph
    assert command[-5:-3] == ("-t", "4")
    assert request.clips[0].duration_seconds == 4
    assert not request.output_path.exists()
    assert source.read_bytes() == original


def test_new_jpegs_and_new_video_render_to_sixteen_point_six_seconds(tmp_path):
    """Actual FFmpeg regression matching the four-clip shape, using only new test media."""
    try:
        renderer = FfmpegTimelineRenderer(
            timeout_seconds=60,
            allowed_input_roots=(tmp_path,),
            allowed_output_roots=(tmp_path,),
        )
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")

    source_video = tmp_path / "new-five-second-video.mp4"
    subprocess.run(
        [
            renderer.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=160x96:r=25:d=5",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source_video),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    stills = []
    for index, color in enumerate(("#176b5f", "#c78f2d", "#9b1c31"), start=1):
        path = tmp_path / f"new-image-{index}.jpg"
        Image.new("RGB", (160, 96), color).save(path, quality=95)
        stills.append(path)
    original_images = [path.read_bytes() for path in stills]
    clips = (
        TimelineClip("new-video", source_video, duration_seconds=5, source_audio="mute"),
        *(
            TimelineClip(f"new-image-{index}", path, kind="image", duration_seconds=4)
            for index, path in enumerate(stills, start=1)
        ),
    )
    # Match the reported task: concat video + image + image, then fade into the
    # final image. A fade immediately after the first clip does not exercise
    # concat's 1/1000000 time base flowing into a 1/25 xfade input.
    clips = (*clips[:3], replace(clips[3], transition="fade"))
    request = TimelineRenderRequest(
        clips=clips,
        output_path=tmp_path / "new-regression-output.mp4",
        width=160,
        height=96,
        fps=25,
    )

    result = renderer.render(request)

    assert result.duration_seconds == pytest.approx(16.6)
    assert result.clip_count == 4
    assert result.fps == 25
    assert result.transitions_applied == ("none", "none", "fade")
    assert result.output_path.is_file() and result.output_path.stat().st_size > 0
    actual_duration, _ = timeline_media._probe(
        result.output_path, renderer.ffprobe, renderer.ffmpeg
    )
    # An encoder/xfade end boundary may omit the final frame, but must not
    # shorten whole image intervals or manufacture an extra repeated tail.
    assert actual_duration == pytest.approx(16.6, abs=1 / request.fps + 0.001)
    decoded = subprocess.run(
        [
            renderer.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-nostats",
            "-progress",
            "pipe:1",
            "-i",
            str(result.output_path),
            "-map",
            "0:v:0",
            "-an",
            "-vsync",
            "0",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    progress = dict(line.split("=", 1) for line in decoded.stdout.splitlines() if "=" in line)
    frames = int(progress["frame"])
    expected_frames = round(16.6 * request.fps)
    assert expected_frames - 1 <= frames <= expected_frames
    assert frames / actual_duration == pytest.approx(request.fps, abs=0.1)
    assert int(progress.get("dup_frames", 0)) == 0
    assert [clip.duration_seconds for clip in request.clips] == [5, 4, 4, 4]
    assert [path.read_bytes() for path in stills] == original_images
