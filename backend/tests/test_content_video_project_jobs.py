import hashlib
import io
import json
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
CONTENT_SRC = ROOT / "services" / "content_service" / "src"
if str(CONTENT_SRC) not in sys.path:
    sys.path.insert(0, str(CONTENT_SRC))

from content_service.db import Base
from content_service.job_leases import cancel_job, claim_job
from content_service.media_adapters import LocalAssetStore, MockSpeechSynthesisPort
from content_service.models import (
    Artifact,
    AssetRef,
    ContentTask,
    Job,
    ProjectAsset,
    ResourceEntry,
    TaskStage,
    VideoProject,
    VideoProjectRevision,
)
from content_service.providers import ProviderStatus, SpeechSynthesisRequest
from content_service.timeline_media import (
    FfmpegTimelineRenderer,
    TimelineRenderRequest,
    TimelineRenderResult,
)
from content_service.video_project_jobs import (
    EXPORT_JOB_KIND,
    RENDER_JOB_KIND,
    run_video_project_export_job,
    run_video_project_render_job,
)


def _id() -> str:
    return str(uuid4())


def _image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 64), "#176b5f").save(output, "PNG")
    return output.getvalue()


def _wav_bytes() -> bytes:
    return (
        MockSpeechSynthesisPort()
        .synthesize(SpeechSynthesisRequest("durable video project job", audio_format="wav"))
        .audio
    )


def _item(
    item_id: str,
    project_asset_id: str | None,
    *,
    start: float,
    duration: float,
    content: str = "",
) -> dict:
    return {
        "id": item_id,
        "project_asset_id": project_asset_id,
        "label": item_id,
        "start_seconds": start,
        "duration_seconds": duration,
        "trim_in_seconds": 0,
        "trim_out_seconds": 0,
        "volume": 1,
        "speed": 1,
        "enabled": True,
        "content": content,
        "transform": {},
        "metadata": {},
    }


def _track(kind: str, items: list[dict]) -> dict:
    return {
        "id": f"track-{kind}",
        "kind": kind,
        "name": kind,
        "muted": False,
        "locked": False,
        "items": items,
    }


@dataclass
class JobFixture:
    session_factory: sessionmaker
    store: LocalAssetStore
    data_root: Path
    task_id: str
    project_id: str
    revision_id: str

    def add_job(self, kind: str, payload: dict, *, status: str = "queued") -> str:
        with self.session_factory() as session:
            job = Job(
                id=_id(),
                task_id=self.task_id,
                owner_id="user-a",
                kind=kind,
                status=status,
                progress=0,
                result_json=json.dumps(payload, ensure_ascii=False),
            )
            session.add(job)
            session.commit()
            return job.id


class RecordingRenderer:
    status = ProviderStatus(
        provider_id="test-timeline-renderer",
        name="recording-renderer",
        capability="timeline-video-composition",
        mode="local",
        external=False,
        enabled=True,
    )

    def __init__(self, *, failure: Exception | None = None, callback=None):
        self.failure = failure
        self.callback = callback
        self.requests: list[TimelineRenderRequest] = []

    def render(self, request: TimelineRenderRequest) -> TimelineRenderResult:
        self.requests.append(request)
        if self.callback is not None:
            self.callback(request)
        if self.failure is not None:
            raise self.failure
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(b"safe-test-mp4")
        duration = sum(float(clip.duration_seconds or 0) for clip in request.clips)
        return TimelineRenderResult(
            output_path=request.output_path,
            duration_seconds=duration,
            width=request.width,
            height=request.height,
            fps=request.fps,
            clip_count=len(request.clips),
            audio_track_count=len(request.audio_tracks),
            transitions_applied=tuple(clip.transition for clip in request.clips[1:]),
            source_audio_clip_count=sum(clip.source_audio == "keep" for clip in request.clips),
            ducking_applied=request.ducking.enabled,
            logos_applied=len(request.logos),
            subtitles_burned=len(request.subtitles),
        )


@pytest.fixture
def job_fixture(tmp_path) -> JobFixture:
    engine = create_engine(f"sqlite:///{tmp_path / 'video-jobs.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    data_root = tmp_path / "content-data"
    store = LocalAssetStore(data_root / "objects")
    task_id = _id()
    project_id = _id()
    revision_id = _id()

    image = store.put_bytes("inputs/shot.png", _image_bytes(), "image/png")
    voice = store.put_bytes("inputs/voice.wav", _wav_bytes(), "audio/wav")
    with factory() as session:
        task = ContentTask(
            id=task_id,
            owner_id="user-a",
            mode="video",
            name="持久视频任务",
        )
        project = VideoProject(
            id=project_id,
            task_id=task_id,
            current_revision=1,
            current_revision_id=revision_id,
            created_by="user-a",
        )
        image_entry = ResourceEntry(
            id=_id(),
            owner_id="user-a",
            resource_kind="image",
            title="shot.png",
            active=True,
        )
        image_ref = AssetRef(
            id=_id(),
            entry_id=image_entry.id,
            storage_key=image.key,
            media_type="image/png",
            size_bytes=image.size,
            checksum=image.checksum_sha256,
            metadata_json="{}",
        )
        voice_artifact = Artifact(
            id=_id(),
            task_id=task_id,
            kind="voice",
            status="confirmed",
            content_json=json.dumps({"media_type": "audio/wav", "duration_seconds": 1.5}),
            storage_key=voice.key,
            created_by="user-a",
        )
        session.add_all([task, project, image_entry, image_ref, voice_artifact])
        session.flush()
        image_asset = ProjectAsset(
            id=_id(),
            project_id=project_id,
            source_type="asset_ref",
            source_id=image_ref.id,
            asset_ref_id=image_ref.id,
            role="video",
            label="主画面",
            metadata_json="{}",
            active=True,
            created_by="user-a",
        )
        voice_asset = ProjectAsset(
            id=_id(),
            project_id=project_id,
            source_type="artifact",
            source_id=voice_artifact.id,
            artifact_id=voice_artifact.id,
            role="voice",
            label="旁白",
            metadata_json="{}",
            active=True,
            created_by="user-a",
        )
        timeline = {
            "canvas": {
                "width": 96,
                "height": 64,
                "fps": 20,
                "aspect_ratio": "3:2",
            },
            "tracks": [
                _track(
                    "video",
                    [_item("clip-one", image_asset.id, start=0, duration=0.8)],
                ),
                _track("overlay", []),
                _track(
                    "subtitle",
                    [
                        _item(
                            "subtitle-one",
                            None,
                            start=0.1,
                            duration=0.5,
                            content="锁定版本字幕",
                        )
                    ],
                ),
                _track(
                    "voice",
                    [_item("voice-one", voice_asset.id, start=0.1, duration=0.6)],
                ),
                _track("music", []),
                _track("sfx", []),
            ],
        }
        revision = VideoProjectRevision(
            id=revision_id,
            project_id=project_id,
            revision=1,
            source="human",
            timeline_json=json.dumps(timeline, ensure_ascii=False),
            operations_json="[]",
            created_by="user-a",
        )
        session.add_all([image_asset, voice_asset, revision])
        session.commit()

    fixture = JobFixture(factory, store, data_root, task_id, project_id, revision_id)
    yield fixture
    engine.dispose()


def _render_payload(fixture: JobFixture, **overrides) -> dict:
    return {
        "project_id": fixture.project_id,
        "revision_id": fixture.revision_id,
        "profile": "preview",
        "fps": 12,
        "subtitle_mode": "none",
        "include_audio": False,
        **overrides,
    }


def test_render_worker_pins_revision_applies_options_and_creates_video_artifact(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(
        RENDER_JOB_KIND,
        {
            **_render_payload(fixture),
            "output_path": "C:/Windows/System32/config/SAM",
            "ffmpeg_filter": "movie=/etc/passwd",
        },
    )
    renderer = RecordingRenderer()

    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )

    assert outcome.status == "succeeded", outcome.error
    assert outcome.artifact_id
    request = renderer.requests[0]
    assert request.fps == 12
    assert request.video_bitrate == "1M"
    assert (request.width, request.height) == (720, 480)
    assert request.audio_tracks == ()
    assert request.subtitles == ()
    assert all(clip.source_audio == "mute" for clip in request.clips)
    assert str(request.output_path).startswith(str(fixture.store.root))
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        artifact = session.get(Artifact, outcome.artifact_id)
        result = json.loads(job.result_json)
        content = json.loads(artifact.content_json)
        assert job.status == "succeeded"
        assert job.progress == 100
        assert job.attempt == 1
        assert job.lease_token is None and job.lease_owner is None
        assert job.completed_at is not None
        assert "output_path" not in result
        assert "ffmpeg_filter" not in result
        assert artifact.kind == "video"
        assert artifact.stage_key == "video_preview"
        assert content["purpose"] == "exact_project_preview"
        assert content["revision_id"] == fixture.revision_id
        assert content["include_audio"] is False
        assert fixture.store.path_for(artifact.storage_key).is_file()


@pytest.mark.parametrize(
    ("profile", "expected_size", "expected_bitrate"),
    [
        ("preview-480p", (720, 480), "1M"),
        ("final-480p", (720, 480), "3M"),
        ("final-720p", (1080, 720), "5M"),
        ("final-1080p", (1620, 1080), "8M"),
    ],
)
def test_render_profiles_preserve_canvas_ratio_with_even_dimensions(
    job_fixture,
    profile,
    expected_size,
    expected_bitrate,
):
    fixture = job_fixture
    job_id = fixture.add_job(
        RENDER_JOB_KIND,
        _render_payload(fixture, profile=profile, fps=30),
    )
    renderer = RecordingRenderer()
    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )
    assert outcome.status == "succeeded", outcome.error
    request = renderer.requests[0]
    assert (request.width, request.height) == expected_size
    assert request.width % 2 == 0 and request.height % 2 == 0
    assert request.width / request.height == pytest.approx(1.5)
    assert request.video_bitrate == expected_bitrate
    if profile.startswith("final-"):
        with fixture.session_factory() as session:
            artifact = session.get(Artifact, outcome.artifact_id)
            stage = session.scalar(
                select(TaskStage).where(
                    TaskStage.task_id == fixture.task_id,
                    TaskStage.stage_key == "video_preview",
                )
            )
            assert artifact.status == "draft"
            assert stage is None


def test_final_render_completion_never_overwrites_concurrent_video_selection(job_fixture):
    fixture = job_fixture
    selected_id = _id()
    stage_id = _id()
    confirmed_at = datetime.now(UTC)
    with fixture.session_factory() as session:
        task = session.get(ContentTask, fixture.task_id)
        task.current_stage = "export"
        task.workflow_enforced = True
        task.version = 17
        session.add(
            Artifact(
                id=selected_id,
                task_id=fixture.task_id,
                kind="video",
                stage_key="video_preview",
                version=1,
                status="confirmed",
                content_json=json.dumps({"purpose": "final_project_render"}),
                created_by="user-b",
            )
        )
        session.add(
            TaskStage(
                id=stage_id,
                task_id=fixture.task_id,
                stage_key="video_preview",
                state="confirmed",
                selected_artifact_ids_json=json.dumps({"video": [selected_id]}),
                confirmed_artifact_ids_json=json.dumps({"video": [selected_id]}),
                version=9,
                confirmed_by="user-b",
                confirmed_at=confirmed_at,
            )
        )
        session.commit()

    job_id = fixture.add_job(
        RENDER_JOB_KIND,
        _render_payload(fixture, profile="final-720p", fps=30),
    )
    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(),
        fixture.data_root,
    )

    assert outcome.status == "succeeded", outcome.error
    with fixture.session_factory() as session:
        rendered = session.get(Artifact, outcome.artifact_id)
        selected = session.get(Artifact, selected_id)
        stage = session.get(TaskStage, stage_id)
        task = session.get(ContentTask, fixture.task_id)
        assert rendered.status == "draft"
        assert selected.status == "confirmed"
        assert json.loads(stage.selected_artifact_ids_json) == {"video": [selected_id]}
        assert json.loads(stage.confirmed_artifact_ids_json) == {"video": [selected_id]}
        assert stage.state == "confirmed"
        assert stage.version == 9
        assert stage.confirmed_by == "user-b"
        assert stage.confirmed_at == confirmed_at
        assert task.current_stage == "export"
        assert task.version == 17
        assert task.workflow_enforced is True


def test_final_1080p_rejects_more_than_60_fps_defense_in_depth(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(
        RENDER_JOB_KIND,
        _render_payload(fixture, profile="final-1080p", fps=61),
    )
    renderer = RecordingRenderer()
    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )
    assert outcome.status == "failed"
    assert "不能超过 60" in outcome.error
    assert renderer.requests == []


def test_soft_subtitle_render_muxes_mov_text_in_controlled_workspace(job_fixture):
    fixture = job_fixture
    try:
        renderer = FfmpegTimelineRenderer(
            timeout_seconds=60,
            allowed_input_roots=(fixture.store.root,),
            allowed_output_roots=(fixture.store.root,),
        )
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")
    job_id = fixture.add_job(
        RENDER_JOB_KIND,
        _render_payload(
            fixture,
            profile="final-480p",
            fps=20,
            subtitle_mode="soft",
            include_audio=True,
        ),
    )

    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )

    assert outcome.status == "succeeded", outcome.error
    output = fixture.store.path_for(outcome.storage_key)
    assert output.stat().st_size > 0
    with fixture.session_factory() as session:
        artifact = session.get(Artifact, outcome.artifact_id)
        content = json.loads(artifact.content_json)
        assert content["subtitle_mode"] == "soft"
        assert content["soft_subtitle_track"] is True
        assert content["subtitles_burned"] == 0
    if renderer.ffprobe:
        probed = subprocess.run(
            [
                renderer.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name",
                "-of",
                "json",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        streams = json.loads(probed.stdout)["streams"]
        assert any(
            stream.get("codec_type") == "subtitle" and stream.get("codec_name") == "mov_text"
            for stream in streams
        )


def test_expired_attempt_is_fenced_and_cannot_complete_or_publish_artifact(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(RENDER_JOB_KIND, _render_payload(fixture))
    reclaimed = {}

    def expire_and_reclaim(_request):
        with fixture.session_factory() as session:
            job = session.get(Job, job_id)
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        with fixture.session_factory() as session:
            second = claim_job(
                session,
                worker_id="replacement-worker",
                job_id=job_id,
            )
            assert second is not None and second.attempt == 2
            reclaimed["token"] = second.token
            session.commit()

    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(callback=expire_and_reclaim),
        fixture.data_root,
    )

    assert outcome.status == "skipped"
    assert "租约" in outcome.error
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        assert job.status == "running"
        assert job.attempt == 2
        assert job.lease_token == reclaimed["token"]
        assert (
            session.scalars(
                select(Artifact).where(
                    Artifact.task_id == fixture.task_id,
                    Artifact.kind == "video",
                )
            ).all()
            == []
        )


@pytest.mark.parametrize("job_kind", [RENDER_JOB_KIND, EXPORT_JOB_KIND])
@pytest.mark.parametrize("posix_path", [False, True])
def test_render_worker_persists_sanitized_failure(job_fixture, job_kind, posix_path):
    fixture = job_fixture
    payload = _render_payload(fixture)
    if job_kind == EXPORT_JOB_KIND:
        payload["deliverables"] = ["mixed_video"]
    job_id = fixture.add_job(job_kind, payload)
    private_path = fixture.data_root / "objects" / "private"
    error_path = private_path.as_posix() if posix_path else str(private_path)
    renderer = RecordingRenderer(failure=RuntimeError(f"FFmpeg failed under {error_path}"))

    worker = (
        run_video_project_render_job
        if job_kind == RENDER_JOB_KIND
        else run_video_project_export_job
    )
    outcome = worker(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )

    assert outcome.status == "failed"
    assert "视频合成" in outcome.error
    assert "FFmpeg" not in outcome.error
    assert "<content-data>" not in outcome.error
    assert "[路径已隐藏]" not in outcome.error
    assert str(fixture.data_root) not in outcome.error
    assert fixture.data_root.as_posix() not in outcome.error
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed"
        assert job.error == outcome.error
        assert json.loads(job.result_json)["worker_state"] == "failed"
        assert json.loads(job.result_json)["error_detail"]["message"] == outcome.error
        assert json.loads(job.result_json)["error_detail"]["code"] == "render_failed"
        assert (
            session.scalars(
                select(Artifact).where(
                    Artifact.task_id == fixture.task_id,
                    Artifact.kind == "video",
                )
            ).all()
            == []
        )


def test_render_worker_still_rejects_html_error_bodies(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(RENDER_JOB_KIND, _render_payload(fixture))
    renderer = RecordingRenderer(
        failure=RuntimeError(
            f"<html>FFmpeg failed under {fixture.data_root / 'objects' / 'private'}</html>"
        )
    )
    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )
    assert outcome.status == "failed"
    assert "FFmpeg" not in outcome.error
    assert "<html>" not in outcome.error
    assert str(fixture.data_root) not in outcome.error
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        detail = json.loads(job.result_json)["error_detail"]
        assert detail["message"] == job.error == outcome.error
        assert detail["code"] == "unknown_error"


def test_render_worker_preserves_specific_chinese_validation_reason(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(RENDER_JOB_KIND, _render_payload(fixture))
    reason = "片段裁切终点超过素材时长，请缩短该片段后重试"
    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(failure=ValueError(reason)),
        fixture.data_root,
    )
    assert outcome.status == "failed"
    assert outcome.error == reason
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        assert job.error == reason
        assert json.loads(job.result_json)["error_detail"]["message"] == reason


def test_render_worker_honors_cancellation_after_renderer_returns(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(RENDER_JOB_KIND, _render_payload(fixture))

    def cancel(_request):
        with fixture.session_factory() as session:
            transition = cancel_job(session, job_id)
            assert transition.status == "running"
            session.commit()

    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(callback=cancel),
        fixture.data_root,
    )

    assert outcome.status == "cancelled", outcome.error
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        assert job.status == "cancelled"
        assert json.loads(job.result_json)["worker_state"] == "cancelled"
        assert (
            session.scalars(
                select(Artifact).where(
                    Artifact.task_id == fixture.task_id,
                    Artifact.kind == "video",
                )
            ).all()
            == []
        )
    assert not any(fixture.store.root.rglob(f"*{job_id}*.mp4"))


def test_invalid_render_payload_fails_without_accepting_paths(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(
        RENDER_JOB_KIND,
        {"project_id": "C:/project", "revision_id": fixture.revision_id},
    )
    outcome = run_video_project_render_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(),
        fixture.data_root,
    )
    assert outcome.status == "failed"
    assert "内部 ID" in outcome.error


def test_export_worker_packages_selected_outputs_manifest_and_checksums(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(
        EXPORT_JOB_KIND,
        {
            "project_id": fixture.project_id,
            "revision_id": fixture.revision_id,
            "deliverables": [
                "mixed_video",
                "silent_video",
                "voice",
                "subtitles",
                "clips",
                "project_zip",
            ],
            "profile": "final",
            "fps": 18,
            "subtitle_mode": "burn",
            "subtitle_format": "both",
            "audio_format": "wav",
            "clip_item_ids": ["clip-one"],
            "include_original_assets": True,
            "raw_command": "ffmpeg -i C:/secret",
        },
    )
    renderer = RecordingRenderer()

    outcome = run_video_project_export_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        renderer,
        fixture.data_root,
    )

    assert outcome.status == "succeeded", outcome.error
    archive_path = fixture.store.path_for(outcome.storage_key)
    assert archive_path.stat().st_size > 0
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert {
            "video/mixed.mp4",
            "video/silent.mp4",
            "audio/voice.wav",
            "subtitles/subtitles.srt",
            "subtitles/subtitles.vtt",
            "clips/001-clip-one.mp4",
            "project/project.zip",
            "manifest.json",
            "checksums.sha256",
        } <= names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["revision_id"] == fixture.revision_id
        assert manifest["fps"] == 18
        assert len(manifest["deliverables"]) == 7
        assert "锁定版本字幕" in archive.read("subtitles/subtitles.srt").decode()
        assert archive.read("subtitles/subtitles.vtt").startswith(b"WEBVTT")
        for line in archive.read("checksums.sha256").decode().splitlines():
            expected, name = line.split("  ", 1)
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        with zipfile.ZipFile(io.BytesIO(archive.read("project/project.zip"))) as project_zip:
            assert {"project.json", "manifest.json", "checksums.sha256"} <= set(
                project_zip.namelist()
            )

    assert len(renderer.requests) == 3
    mixed, silent, clip = renderer.requests
    assert mixed.audio_tracks and mixed.subtitles
    assert silent.audio_tracks == ()
    assert all(item.source_audio == "mute" for item in silent.clips)
    assert clip.audio_tracks == () and clip.subtitles == ()
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        result = json.loads(job.result_json)
        assert job.status == "succeeded"
        assert result["worker_state"] == "succeeded"
        assert "raw_command" not in result
        assert result["storage_key"] == outcome.storage_key


def test_export_worker_returns_direct_mp4_for_single_mixed_video(job_fixture):
    fixture = job_fixture
    job_id = fixture.add_job(
        EXPORT_JOB_KIND,
        {
            "project_id": fixture.project_id,
            "revision_id": fixture.revision_id,
            "deliverables": ["mixed_video"],
            "profile": "final-720p",
            "fps": 30,
            "subtitle_mode": "burn",
        },
    )

    outcome = run_video_project_export_job(
        job_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(),
        fixture.data_root,
    )

    assert outcome.status == "succeeded", outcome.error
    output_path = fixture.store.path_for(outcome.storage_key)
    assert output_path.suffix == ".mp4"
    assert output_path.stat().st_size > 0
    with fixture.session_factory() as session:
        job = session.get(Job, job_id)
        result = json.loads(job.result_json)
        assert result["media_type"] == "video/mp4"
        assert result["download_filename"] == "成片视频.mp4"
        assert result["manifest"]["deliverables"][0]["kind"] == "mixed_mp4"


def test_export_worker_rejects_unknown_deliverable_and_preserves_cancel(job_fixture):
    fixture = job_fixture
    invalid_id = fixture.add_job(
        EXPORT_JOB_KIND,
        {
            "project_id": fixture.project_id,
            "revision_id": fixture.revision_id,
            "deliverables": ["raw_ffmpeg"],
        },
    )
    invalid = run_video_project_export_job(
        invalid_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(),
        fixture.data_root,
    )
    assert invalid.status == "failed"
    assert "导出类型" in invalid.error

    cancelled_id = fixture.add_job(
        EXPORT_JOB_KIND,
        {
            "project_id": fixture.project_id,
            "revision_id": fixture.revision_id,
            "deliverables": ["mixed_mp4"],
        },
        status="cancelled",
    )
    cancelled = run_video_project_export_job(
        cancelled_id,
        fixture.session_factory,
        fixture.store,
        RecordingRenderer(),
        fixture.data_root,
    )
    assert cancelled.status == "cancelled"
    with fixture.session_factory() as session:
        assert session.get(Job, cancelled_id).status == "cancelled"
