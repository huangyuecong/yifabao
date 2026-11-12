import io
import json
import math
import struct
import subprocess
import wave
from dataclasses import replace
from uuid import uuid4

import pytest
from PIL import Image
from test_content_video_project_jobs import (
    _render_payload,
)
from test_content_video_project_jobs import (
    job_fixture as isolated_job_fixture,
)

from content_service.models import (
    Artifact,
    AssetRef,
    ContentTask,
    ProjectAsset,
    VideoProject,
    VideoProjectRevision,
)
from content_service.schemas import VideoProjectExportRequest, VideoProjectRenderRequest
from content_service.timeline_media import (
    FfmpegTimelineRenderer,
    TimelineLogo,
    _probe,
    _subtitle_force_style,
)
from content_service.video_project_jobs import (
    RENDER_JOB_KIND,
    _apply_render_controls,
    run_video_project_render_job,
)
from content_service.video_project_rendering import (
    VideoProjectRenderingError,
    build_stem_export_plan,
    prepare_video_project_render,
)

job_fixture = isolated_job_fixture


def _tone(seconds):
    buffer = io.BytesIO()
    rate = 32_000
    period = b"".join(
        struct.pack("<h", round(6000 * math.sin(2 * math.pi * i / 80))) for i in range(80)
    )
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((period * math.ceil(seconds * rate / 80))[: round(seconds * rate) * 2])
    return buffer.getvalue()


def _renderer(fixture):
    return FfmpegTimelineRenderer(
        timeout_seconds=60,
        allowed_input_roots=(fixture.store.root,),
        allowed_output_roots=(fixture.store.root,),
    )


def _set_media(fixture, *, video_seconds=5, voice_seconds=14.66):
    renderer = _renderer(fixture)
    video = fixture.store.root / "inputs" / "main-source.mp4"
    subprocess.run(
        [
            renderer.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x143040:s=160x96:r=24:d={video_seconds}",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    stored_video = fixture.store.put_bytes("inputs/main.mp4", video.read_bytes(), "video/mp4")
    stored_voice = fixture.store.put_bytes("inputs/voice.wav", _tone(voice_seconds), "audio/wav")
    with fixture.session_factory() as session:
        revision = session.get(VideoProjectRevision, fixture.revision_id)
        timeline = json.loads(revision.timeline_json)
        video_item = next(track for track in timeline["tracks"] if track["kind"] == "video")[
            "items"
        ][0]
        video_asset = session.get(ProjectAsset, video_item["project_asset_id"])
        ref = session.get(AssetRef, video_asset.asset_ref_id)
        ref.storage_key, ref.media_type = stored_video.key, "video/mp4"
        ref.size_bytes, ref.checksum = stored_video.size, stored_video.checksum_sha256
        ref.metadata_json = json.dumps(
            {"probe": {"duration_seconds": video_seconds, "audio_codec": ""}}
        )
        voice_item = next(track for track in timeline["tracks"] if track["kind"] == "voice")[
            "items"
        ][0]
        voice_asset = session.get(ProjectAsset, voice_item["project_asset_id"])
        voice_artifact = session.get(Artifact, voice_asset.artifact_id)
        voice_artifact.storage_key = stored_voice.key
        voice_artifact.content_json = json.dumps(
            {"media_type": "audio/wav", "duration_seconds": voice_seconds}
        )
        timeline["canvas"] = {"width": 160, "height": 96, "fps": 24, "aspect_ratio": "5:3"}
        video_item["duration_seconds"] = video_seconds
        voice_item.update(start_seconds=0, duration_seconds=voice_seconds)
        subtitle = next(track for track in timeline["tracks"] if track["kind"] == "subtitle")[
            "items"
        ][0]
        subtitle.update(start_seconds=9, duration_seconds=4.2, content="TAIL")
        subtitle["metadata"] = {
            "subtitle_style": {
                "font_size": 12,
                "primary_color": "#FF0000",
                "outline_width": 0,
                "margin_v": 8,
            }
        }
        revision.timeline_json = json.dumps(timeline)
        session.commit()
    return renderer


def _prepared(fixture, session):
    return prepare_video_project_render(
        session,
        session.get(VideoProject, fixture.project_id),
        fixture.store,
        fixture.store.root / "test-output.mp4",
        work_root=fixture.store.root / "work",
        revision_id=fixture.revision_id,
    )


@pytest.mark.parametrize(("mode", "subtitle_mode"), [("full", "burn"), ("video", "soft")])
def test_final_video_keeps_late_audio_subtitles_and_pinned_revision(
    job_fixture, mode, subtitle_mode
):
    fixture = job_fixture
    renderer = _set_media(fixture)
    job_id = fixture.add_job(
        RENDER_JOB_KIND,
        _render_payload(
            fixture,
            profile="final-480p",
            fps=24,
            subtitle_mode=subtitle_mode,
            include_audio=True,
        ),
    )
    # A later edit must not leak into this already-submitted final render.
    with fixture.session_factory() as session:
        session.get(ContentTask, fixture.task_id).mode = mode
        project = session.get(VideoProject, fixture.project_id)
        original = session.get(VideoProjectRevision, fixture.revision_id)
        changed = json.loads(original.timeline_json)
        for track in changed["tracks"]:
            if track["kind"] != "video":
                track["items"] = []
            else:
                track["items"][0]["duration_seconds"] = 1
        later = VideoProjectRevision(
            id=str(uuid4()),
            project_id=project.id,
            revision=2,
            source="human",
            timeline_json=json.dumps(changed),
            operations_json="[]",
            created_by="user-a",
        )
        session.add(later)
        project.current_revision, project.current_revision_id = 2, later.id
        session.commit()
    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )
    assert outcome.status == "succeeded", outcome.error
    output = fixture.store.path_for(outcome.storage_key)
    duration, has_audio = _probe(output, renderer.ffprobe, renderer.ffmpeg)
    assert duration == pytest.approx(14.66, abs=0.08)
    assert has_audio is True
    with fixture.session_factory() as session:
        artifact = session.get(Artifact, outcome.artifact_id)
        content = json.loads(artifact.content_json)
        assert artifact.kind == "video"
        assert content["purpose"] == "final_project_render"
        assert content["timeline_composition_version"] == 2
        assert content["revision_id"] == fixture.revision_id
        assert content["revision"] == 1
        assert content["fps"] == 24
        assert content["subtitle_mode"] == subtitle_mode
    # Decode the actual MP4 after the original 5-second clip ended.
    pcm = subprocess.run(
        [
            renderer.ffmpeg,
            "-v",
            "error",
            "-ss",
            "10",
            "-i",
            str(output),
            "-t",
            "0.15",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    assert samples and max(abs(value) for value in samples) > 1000
    raw = subprocess.run(
        [
            renderer.ffmpeg,
            "-v",
            "error",
            "-ss",
            "10",
            "-i",
            str(output),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    frame = Image.frombytes("RGB", (800, 480), raw)
    assert (
        frame.getpixel((10, 10))[2] > frame.getpixel((10, 10))[0]
    )  # frozen source remains visible
    red_pixels = sum(
        red > 130 and red > green * 2 and red > blue * 2 for red, green, blue in frame.getdata()
    )
    if subtitle_mode == "burn":
        assert red_pixels > 100  # late caption is visibly burned after t=5
    else:
        assert red_pixels == 0
        captions = subprocess.run(
            [
                renderer.ffmpeg,
                "-v",
                "error",
                "-i",
                str(output),
                "-map",
                "0:s:0",
                "-f",
                "srt",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout.decode("utf-8")
        assert "00:00:09,000 --> 00:00:13,200" in captions
        assert "TAIL" in captions


def test_source_tail_trims_bound_video_audio_and_reject_exhausted_sources(job_fixture):
    fixture = job_fixture
    renderer = _set_media(fixture, voice_seconds=4)
    with fixture.session_factory() as session:
        revision = session.get(VideoProjectRevision, fixture.revision_id)
        timeline = json.loads(revision.timeline_json)
        clip = next(track for track in timeline["tracks"] if track["kind"] == "video")["items"][0]
        voice = next(track for track in timeline["tracks"] if track["kind"] == "voice")["items"][0]
        clip.update(trim_in_seconds=1, trim_out_seconds=1, duration_seconds=5)
        voice.update(trim_in_seconds=1, trim_out_seconds=1, duration_seconds=4, speed=2)
        revision.timeline_json = json.dumps(timeline)
        session.flush()
        with _prepared(fixture, session) as prepared:
            assert prepared.request.clips[0].duration_seconds == pytest.approx(3)
            assert prepared.request.clips[0].source_out_seconds == pytest.approx(4)
            audio = prepared.request.audio_tracks[0]
            assert audio.duration_seconds == pytest.approx(1)
            assert audio.source_out_seconds == pytest.approx(3)
            command = renderer.build_command(prepared.request)
            graph = command[command.index("-filter_complex") + 1]
            assert "atrim=start=1:duration=2" in graph
            assert "atempo=2" in graph
        clip["trim_out_seconds"] = 5
        revision.timeline_json = json.dumps(timeline)
        session.flush()
        with pytest.raises(VideoProjectRenderingError, match="裁入与裁出合计"):
            _prepared(fixture, session)


def test_image_and_caption_legacy_trims_are_ignored_but_duration_is_preserved(job_fixture):
    fixture = job_fixture
    with fixture.session_factory() as session:
        revision = session.get(VideoProjectRevision, fixture.revision_id)
        timeline = json.loads(revision.timeline_json)
        for track in timeline["tracks"]:
            if track["kind"] in {"video", "subtitle"}:
                track["items"][0].update(trim_in_seconds=3, trim_out_seconds=5)
        subtitle = next(track for track in timeline["tracks"] if track["kind"] == "subtitle")[
            "items"
        ][0]
        subtitle.update(start_seconds=5, duration_seconds=3)
        revision.timeline_json = json.dumps(timeline)
        session.flush()
        with _prepared(fixture, session) as prepared:
            assert prepared.request.clips[0].source_in_seconds == 0
            assert prepared.request.clips[0].duration_seconds == pytest.approx(0.8)
            assert prepared.request.duration_seconds == 8
            assert (
                "00:00:05,000 --> 00:00:08,000"
                in prepared.request.subtitles[0].source_path.read_text()
            )
            command = _renderer(fixture).build_command(prepared.request)
            assert command[command.index("-t") + 1] == "0.8"  # input image length remains unchanged
            assert (
                "tpad=stop_mode=clone:stop_duration=7.2"
                in command[command.index("-filter_complex") + 1]
            )


def test_looping_trimmed_audio_and_stem_use_only_the_trimmed_source(job_fixture):
    fixture = job_fixture
    renderer = _set_media(fixture, voice_seconds=4)
    with fixture.session_factory() as session:
        revision = session.get(VideoProjectRevision, fixture.revision_id)
        timeline = json.loads(revision.timeline_json)
        voice = next(track for track in timeline["tracks"] if track["kind"] == "voice")["items"][0]
        voice.update(trim_in_seconds=1, trim_out_seconds=1, duration_seconds=10)
        voice["metadata"] = {"loop": True}
        revision.timeline_json = json.dumps(timeline)
        session.flush()
        with _prepared(fixture, session) as prepared:
            audio = prepared.request.audio_tracks[0]
            assert audio.loop_source_duration_seconds == pytest.approx(2)
            for command in [
                renderer.build_command(prepared.request),
                build_stem_export_plan(
                    prepared,
                    "voice",
                    fixture.store.root / "voice-stem.wav",
                    allowed_input_roots=(fixture.store.root,),
                    allowed_output_roots=(fixture.store.root,),
                ).command,
            ]:
                graph = command[command.index("-filter_complex") + 1]
                assert "atrim=start=1:duration=2" in graph
                assert "aloop=loop=-1:size=96000" in graph
                assert "-stream_loop" not in command


def test_silent_export_keeps_timeline_length_and_muted_items_do_not_extend_it(job_fixture):
    fixture = job_fixture
    renderer = _set_media(fixture)
    with fixture.session_factory() as session:
        with _prepared(fixture, session) as prepared:
            silent = _apply_render_controls(
                prepared.request,
                profile="final-480p",
                fps=24,
                subtitle_mode="burn",
                include_audio=False,
                output_path=fixture.store.root / "silent.mp4",
            )
            assert silent.duration_seconds == pytest.approx(14.66)
            assert silent.audio_tracks == ()
            graph = renderer.build_command(silent)
            assert (
                "tpad=stop_mode=clone:stop_duration=9.66"
                in graph[graph.index("-filter_complex") + 1]
            )
        revision = session.get(VideoProjectRevision, fixture.revision_id)
        timeline = json.loads(revision.timeline_json)
        for track in timeline["tracks"]:
            if track["kind"] == "voice":
                track["muted"] = True
            if track["kind"] == "subtitle":
                track["items"][0]["enabled"] = False
        revision.timeline_json = json.dumps(timeline)
        session.flush()
        with _prepared(fixture, session) as prepared:
            assert prepared.request.duration_seconds is None
            assert prepared.request.audio_tracks == ()
            graph = renderer.build_command(prepared.request)
            assert "tpad=" not in graph[graph.index("-filter_complex") + 1]


def test_canvas_subtitle_style_and_profile_scaled_overlay(job_fixture):
    fixture = job_fixture
    with fixture.session_factory() as session, _prepared(fixture, session) as prepared:
        subtitle = prepared.request.subtitles[0]
        style = _subtitle_force_style(
            {"font_size": 8}, subtitle.canvas_width, subtitle.canvas_height
        )
        assert "PlayResX=96,PlayResY=64" in style
        assert "FontSize=8" in style
        assert "FontSize=28" in _subtitle_force_style({}, 1280, 720)
        request = replace(
            prepared.request,
            logos=(TimelineLogo("overlay", fixture.store.root / "inputs/shot.png", width=32),),
        )
        controlled = _apply_render_controls(
            request,
            profile="final-480p",
            fps=24,
            subtitle_mode="burn",
            include_audio=True,
            output_path=fixture.store.root / "scaled.mp4",
        )
        assert controlled.logos[0].width == round(32 * controlled.width / 96)
        assert controlled.subtitles[0].canvas_width == 96


def test_real_subtitle_size_is_canvas_relative_at_two_output_profiles(job_fixture):
    fixture = job_fixture
    renderer = _renderer(fixture)
    with fixture.session_factory() as session:
        revision = session.get(VideoProjectRevision, fixture.revision_id)
        timeline = json.loads(revision.timeline_json)
        timeline["canvas"] = {"width": 160, "height": 96, "fps": 24, "aspect_ratio": "5:3"}
        subtitle = next(track for track in timeline["tracks"] if track["kind"] == "subtitle")[
            "items"
        ][0]
        subtitle.update(start_seconds=0, duration_seconds=0.6, content="BBOX")
        subtitle["metadata"] = {
            "subtitle_style": {
                "font_size": 8,
                "primary_color": "#FF0000",
                "outline_width": 0,
                "margin_v": 8,
            }
        }
        revision.timeline_json = json.dumps(timeline)
        session.flush()
        boxes = []
        with _prepared(fixture, session) as prepared:
            for profile in ["final-480p", "final-720p"]:
                request = _apply_render_controls(
                    prepared.request,
                    profile=profile,
                    fps=24,
                    subtitle_mode="burn",
                    include_audio=False,
                    output_path=fixture.store.root / f"font-{profile}.mp4",
                )
                result = renderer.render(request)
                raw = subprocess.run(
                    [
                        renderer.ffmpeg,
                        "-v",
                        "error",
                        "-ss",
                        "0.3",
                        "-i",
                        str(result.output_path),
                        "-frames:v",
                        "1",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgb24",
                        "pipe:1",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                ).stdout
                frame = Image.frombytes("RGB", (request.width, request.height), raw)
                points = [
                    (index % request.width, index // request.width)
                    for index, (red, green, blue) in enumerate(frame.getdata())
                    if red > 130 and red > green * 2 and red > blue * 2
                ]
                assert points
                left, right = min(x for x, _ in points), max(x for x, _ in points)
                top, bottom = min(y for _, y in points), max(y for _, y in points)
                box = (
                    left / request.width,
                    top / request.height,
                    (right - left + 1) / request.width,
                    (bottom - top + 1) / request.height,
                )
                # If PlayRes were ignored, the default 384x288 subtitle canvas
                # would make these 8px glyphs roughly one third as tall.
                assert 0.04 < box[3] < 0.10, box
                assert 0.08 < box[2] < 0.25, box
                boxes.append(box)
        assert boxes[0] == pytest.approx(boxes[1], abs=0.006)


@pytest.mark.parametrize("fps", [1, 12, 24, 25, 48, 60])
def test_project_preview_and_export_accept_actual_canvas_fps(fps):
    assert (
        VideoProjectRenderRequest(expected_revision=1, profile="preview-480p", fps=fps).fps == fps
    )
    assert VideoProjectExportRequest(expected_revision=1, video_profile="480p", fps=fps).fps == fps


@pytest.mark.parametrize("mode", ["full", "video"])
def test_final_confirmation_requires_composed_current_project_revision(
    job_fixture, content_client, mode
):
    from fastapi import HTTPException

    from content_service.main import require_current_video_project_render

    fixture = job_fixture
    with fixture.session_factory() as session:
        task = session.get(ContentTask, fixture.task_id)
        task.mode = mode
        content = {
            "purpose": "final_project_render",
            "timeline_composition_version": 2,
            "project_id": fixture.project_id,
            "revision_id": fixture.revision_id,
        }
        final = Artifact(
            task_id=task.id,
            kind="video",
            content_json=json.dumps(content),
            version=1,
            created_by="user-a",
        )
        require_current_video_project_render(session, task, [final])
        for changes, message in [
            ({"timeline_composition_version": 1}, "完整时间线"),
            ({"purpose": "exact_project_preview"}, "完整时间线"),
            ({"project_id": str(uuid4())}, "完整时间线"),
            ({"revision_id": str(uuid4())}, "视频工程已修改"),
        ]:
            final.content_json = json.dumps({**content, **changes})
            with pytest.raises(HTTPException) as caught:
                require_current_video_project_render(session, task, [final])
            assert caught.value.status_code == 409
            assert message in caught.value.detail
        final.kind = "ai_video_clip"
        with pytest.raises(HTTPException, match="AI 视频候选不是最终成片"):
            require_current_video_project_render(session, task, [final])
