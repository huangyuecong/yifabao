import base64
import hashlib
import io
import json
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from content_service.db import Base
from content_service.hiagent_adapters import HiAgentImageGenerationPort
from content_service.job_leases import claim_job, complete_job
from content_service.llm_adapters import ProviderCallError
from content_service.media_adapters import (
    LocalSubtitleAlignmentPort,
    VolcengineArkAiVideoGenerationPort,
)
from content_service.models import (
    Artifact,
    ContentTask,
    Job,
    ProjectAsset,
    TaskStage,
    VideoProject,
)
from content_service.providers import (
    AiVideoGenerationRequest,
    AiVideoTask,
    AssetWriteResult,
    GeneratedAsset,
    ImageGenerationRequest,
    ProviderMediaPublication,
    ProviderStatus,
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    WordTiming,
)
from content_service.schemas import (
    VideoProjectAiVideoGenerationRequest,
    VideoProjectImageGenerationRequest,
    VideoProjectSubtitleGenerationRequest,
)
from content_service.services import dump, load, project_asset_json
from content_service.video_project_generation import (
    ProjectAiVideoGenerationSpec,
    ProjectImageGenerationSpec,
    ProjectSubtitleGenerationSpec,
    ProjectVoiceGenerationSpec,
    VideoProjectGenerationConflictError,
    VideoProjectGenerationValidationError,
    archive_project_ai_video_result,
    enqueue_project_ai_video,
    generate_project_image_candidates,
    generate_project_subtitle_asset,
    generate_project_voice_asset,
    maybe_retry_ai_video_on_fast,
    release_provider_media_publications,
)


class LocalAssetStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path_for(self, key):
        path = (self.root / hashlib.sha256(key.encode("utf-8")).hexdigest()).resolve()
        assert path == self.root or self.root in path.parents
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put_bytes(self, key, content, media_type):
        path = self.path_for(key)
        path.write_bytes(content)
        return AssetWriteResult(
            key=key,
            size=len(content),
            media_type=media_type,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        )

    def put_stream(self, key, stream, media_type):
        return self.put_bytes(key, stream.read(), media_type)

    def read_bytes(self, key):
        return self.path_for(key).read_bytes()

    def exists(self, key):
        return self.path_for(key).is_file()

    def provider_url(self, key):
        if not self.exists(key):
            return None
        return f"https://objects.example/{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * 160)
    return output.getvalue()


PNG = b"\x89PNG\r\n\x1a\n" + b"generated-image-content"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"generated-video-content"


class FakeSpeechProvider:
    status = ProviderStatus(
        provider_id="speech-provider",
        name="speech",
        capability="speech-synthesis",
        mode="api",
        external=True,
        enabled=True,
        model="speech-model-v2",
    )
    capabilities: ClassVar[dict] = {}

    def __init__(self):
        self.requests = []

    def synthesize(self, request: SpeechSynthesisRequest):
        self.requests.append(request)
        return SpeechSynthesisResult(
            audio=wav_bytes(),
            media_type="audio/wav",
            extension="wav",
            duration_seconds=0.01,
            provider_id=self.status.provider_id,
            voice=request.voice or "default-voice",
            word_timings=(WordTiming("好", 0, 0.01),),
            metadata={
                "usage": {"characters": len(request.text)},
                "cost": {"amount": "0.01", "currency": "CNY"},
                "api_token": "must-not-be-persisted",
            },
        )


class FakeImageProvider:
    status = ProviderStatus(
        provider_id="image-provider",
        name="image",
        capability="image-generation",
        mode="api",
        external=True,
        enabled=True,
        model="image-model-v3",
    )
    capabilities: ClassVar[dict] = {"count": {"max": 8}}

    def __init__(self, *, corrupt=False):
        self.requests = []
        self.corrupt = corrupt

    def generate(self, request: ImageGenerationRequest):
        self.requests.append(request)
        return tuple(
            GeneratedAsset(
                content=b"not-an-image" if self.corrupt else PNG + bytes([index]),
                media_type="image/png",
                extension="png",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
                metadata={
                    "candidate_index": index,
                    "usage": {"generated_images": 1},
                    "authorization": "Bearer secret",
                },
            )
            for index in range(request.count)
        )


class FakeAiVideoProvider:
    status = ProviderStatus(
        provider_id="video-provider",
        name="video",
        capability="ai-video-generation",
        mode="api",
        external=True,
        enabled=True,
        model="video-model-v4",
    )
    capabilities: ClassVar[dict] = {
        "async": True,
        "text_to_video": True,
        "image_to_video": True,
        "reference_images": {"max": 9},
        "reference_videos": {"max": 3, "total_duration_seconds": 15},
        "durations": [5, 10, 15],
        "ratios": ["16:9", "9:16", "1:1"],
        "resolutions": ["480p", "720p"],
        "generate_audio": True,
        "return_last_frame": True,
    }

    def __init__(self):
        self.requests = []

    def submit(self, request: AiVideoGenerationRequest):
        self.requests.append(request)
        return AiVideoTask(
            provider_task_id="provider-task-1",
            status="queued",
            provider_id=self.status.provider_id,
            model=self.status.model or "",
            progress=0,
            metadata={"usage": {"submitted": 1}, "secret": "hidden"},
        )

    def get(self, provider_task_id):
        raise AssertionError("纯服务不应轮询 provider")

    def cancel(self, provider_task_id):
        raise AssertionError("本测试不取消 provider")


class ProviderManagedOutputAiVideoProvider(FakeAiVideoProvider):
    capabilities: ClassVar[dict] = {
        **FakeAiVideoProvider.capabilities,
        "reference_videos": {
            **FakeAiVideoProvider.capabilities["reference_videos"],
            "provider_native_reference_supported": True,
            "provider_native_reference_transport": "provider-managed-output-https",
        },
    }

    def __init__(self, *, source_status="succeeded", source_video_url=None):
        super().__init__()
        self.source_status = source_status
        self.source_video_url = source_video_url
        self.get_calls = []

    def get(self, provider_task_id):
        self.get_calls.append(provider_task_id)
        return AiVideoTask(
            provider_task_id=provider_task_id,
            status=self.source_status,
            provider_id=self.status.provider_id,
            model=self.status.model or "",
            progress=100 if self.source_status == "succeeded" else None,
            video_url=self.source_video_url,
        )


class FakeProviderMediaPublisher:
    enabled = True
    transport = "tos-private-presigned-https"
    url_ttl_seconds = 600

    def __init__(self):
        self.published = []
        self.released = []

    def publish(self, source_path, *, media_type, publication_id):
        index = len(self.published) + 1
        token = f"provider-media/reference-videos/2000/job/{index}.mp4"
        publication = ProviderMediaPublication(
            url=f"https://private.example/{index}.mp4?X-Tos-Signature=secret-{index}",
            transport=self.transport,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.url_ttl_seconds),
            cleanup_token=token,
        )
        self.published.append(
            {
                "source_path": source_path,
                "media_type": media_type,
                "publication_id": publication_id,
                "publication": publication,
            }
        )
        return publication

    def release(self, cleanup_token):
        self.released.append(cleanup_token)

    def purge_expired(self):
        return 0


@pytest.fixture
def generation_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'generation.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    store = LocalAssetStore(tmp_path / "assets")
    try:
        with session_factory() as session:
            task = ContentTask(
                owner_id="owner-a",
                mode="video",
                name="工程素材生成",
                topic="品牌短视频",
            )
            session.add(task)
            session.flush()
            project = VideoProject(
                task_id=task.id,
                current_revision=1,
                current_revision_id=str(uuid4()),
                created_by="owner-a",
            )
            session.add(project)
            session.commit()
            task_id = task.id
            project_id = project.id
        yield session_factory, store, task_id, project_id
    finally:
        engine.dispose()


def context(session, task_id, project_id):
    return session.get(ContentTask, task_id), session.get(VideoProject, project_id)


def test_video_generation_request_schema_declares_reference_limits():
    image_schema = VideoProjectImageGenerationRequest.model_json_schema()
    video_schema = VideoProjectAiVideoGenerationRequest.model_json_schema()

    assert image_schema["properties"]["reference_asset_ids"]["maxItems"] == 9
    source_copy_schema = video_schema["properties"]["source_copy_artifact_id"]
    assert any(item.get("maxLength") == 36 for item in source_copy_schema["anyOf"])
    assert video_schema["properties"]["reference_image_asset_ids"]["maxItems"] == 9
    assert video_schema["properties"]["reference_video_asset_ids"]["maxItems"] == 3
    with pytest.raises(ValueError, match="最多允许 9"):
        VideoProjectAiVideoGenerationRequest(
            expected_revision=1,
            prompt="测试",
            source_project_asset_id="source",
            reference_image_asset_ids=[f"image-{index}" for index in range(9)],
        )


def test_volcengine_ai_video_declares_provider_managed_output_reference_support():
    provider = VolcengineArkAiVideoGenerationPort(
        provider_id="volcengine-ai-video",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="test-only",
        model="seedance-test",
        timeout_seconds=10,
    )

    references = provider.capabilities["reference_videos"]
    assert references["provider_native_reference_supported"] is True
    assert references["provider_native_reference_transport"] == ("provider-managed-output-https")
    assert provider.capabilities["reference_media"]["videos"] == references


def test_project_subtitle_request_requires_text_or_audio_source():
    with pytest.raises(ValueError, match="text 或 source_project_asset_id"):
        VideoProjectSubtitleGenerationRequest(expected_revision=1)


def test_project_subtitles_reuse_generated_voice_word_timings_and_create_srt_asset(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    speech_provider = FakeSpeechProvider()
    subtitle_provider = LocalSubtitleAlignmentPort()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        voice = generate_project_voice_asset(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=speech_provider,
            asset_store=store,
            spec=ProjectVoiceGenerationSpec(text="好", audio_format="wav"),
        )
        subtitle = generate_project_subtitle_asset(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=subtitle_provider,
            asset_store=store,
            spec=ProjectSubtitleGenerationSpec(
                source_project_asset_id=voice.project_asset.id,
            ),
        )
        session.commit()

        assert subtitle.project_asset.role == "subtitle"
        assert subtitle.source_project_asset_id == voice.project_asset.id
        assert subtitle.text == "好"
        assert subtitle.segments[0].timing_source == "provider"
        assert store.read_bytes(subtitle.artifact.storage_key).decode("utf-8") == (
            "1\n00:00:00,000 --> 00:00:00,010\n好\n"
        )
        artifact_content = load(subtitle.artifact.content_json)
        assert artifact_content["source_artifact_id"] == voice.artifact.id
        assert artifact_content["source_project_asset_id"] == voice.project_asset.id
        assert artifact_content["provenance"]["request"]["word_timings_used"] is True
        asset_metadata = load(subtitle.project_asset.metadata_json)
        assert asset_metadata["media_kind"] == "subtitle"
        assert asset_metadata["text"] == "好"
        assert asset_metadata["segments"] == artifact_content["segments"]


def test_project_subtitles_require_text_for_uploaded_audio_without_transcript(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    subtitle_provider = LocalSubtitleAlignmentPort()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        stored = store.put_bytes(
            f"tasks/{task.id}/audio/uploaded.wav",
            wav_bytes(),
            "audio/wav",
        )
        audio_artifact = Artifact(
            task_id=task.id,
            kind="voice",
            content_json=dump(
                {
                    "media_type": "audio/wav",
                    "duration_seconds": 2,
                }
            ),
            storage_key=stored.key,
            created_by="owner-a",
        )
        session.add(audio_artifact)
        session.flush()
        audio_asset = ProjectAsset(
            project_id=project.id,
            source_type="artifact",
            source_id=audio_artifact.id,
            artifact_id=audio_artifact.id,
            role="voice",
            label="上传配音",
            metadata_json=dump(
                {
                    "media_kind": "audio",
                    "media_type": "audio/wav",
                    "duration_seconds": 2,
                }
            ),
            created_by="owner-a",
        )
        session.add(audio_asset)
        session.flush()

        with pytest.raises(VideoProjectGenerationValidationError, match="不执行语音转写"):
            generate_project_subtitle_asset(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=subtitle_provider,
                asset_store=store,
                spec=ProjectSubtitleGenerationSpec(
                    source_project_asset_id=audio_asset.id,
                ),
            )

        generated = generate_project_subtitle_asset(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=subtitle_provider,
            asset_store=store,
            spec=ProjectSubtitleGenerationSpec(
                source_project_asset_id=audio_asset.id,
                text="上传音频对应的字幕文本。",
            ),
        )
        assert generated.duration_seconds == 2
        assert all(segment.timing_source == "estimated" for segment in generated.segments)


def create_video_reference_asset(session, store, task, project, *, index, duration_seconds):
    storage_key = f"tasks/{task.id}/references/reference-{index}.mp4"
    stored = store.put_bytes(storage_key, MP4 + bytes([index]), "video/mp4")
    artifact = Artifact(
        task_id=task.id,
        kind="video",
        content_json=dump(
            {
                "media_type": "video/mp4",
                "duration_seconds": duration_seconds,
            }
        ),
        storage_key=stored.key,
        created_by="owner-a",
    )
    session.add(artifact)
    session.flush()
    project_asset = ProjectAsset(
        project_id=project.id,
        source_type="artifact",
        source_id=artifact.id,
        artifact_id=artifact.id,
        role="video",
        label=f"参考视频 {index}",
        metadata_json=dump(
            {
                "media_kind": "video",
                "media_type": "video/mp4",
                "size_bytes": stored.size,
                "duration_seconds": duration_seconds,
            }
        ),
        created_by="owner-a",
    )
    session.add(project_asset)
    session.flush()
    return project_asset


def create_ai_generated_video_reference_asset(
    session,
    store,
    task,
    project,
    *,
    provider_id="video-provider",
    provider_task_id="source-provider-task-1",
    duration_seconds=4,
):
    storage_key = f"tasks/{task.id}/references/{provider_task_id}.mp4"
    stored = store.put_bytes(storage_key, MP4 + b"-provider-output", "video/mp4")
    provenance = {
        "provider_id": provider_id,
        "request": {"duration_seconds": duration_seconds},
    }
    artifact = Artifact(
        task_id=task.id,
        kind="ai_video_clip",
        content_json=dump(
            {
                "source": "ai-generated",
                "provider_task_id": provider_task_id,
                "media_type": "video/mp4",
                "provenance": provenance,
            }
        ),
        storage_key=stored.key,
        created_by="owner-a",
    )
    session.add(artifact)
    session.flush()
    project_asset = ProjectAsset(
        project_id=project.id,
        source_type="artifact",
        source_id=artifact.id,
        artifact_id=artifact.id,
        role="video",
        label="已有 AI 生成视频",
        metadata_json=dump(
            {
                "source": "ai-generated",
                "provider_task_id": provider_task_id,
                "media_type": "video/mp4",
                "size_bytes": stored.size,
                # Deliberately mirror historical rows: duration lives only in
                # the recorded request rather than at metadata top-level.
                "provenance": provenance,
            }
        ),
        created_by="owner-a",
    )
    session.add(project_asset)
    session.flush()
    return project_asset


def test_project_tts_is_owned_versioned_and_records_redacted_cost_provenance(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeSpeechProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        first = generate_project_voice_asset(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectVoiceGenerationSpec(
                text="雅酿酒业，敬每一份努力。",
                voice="narrator-a",
                instruction="沉稳、真诚",
            ),
        )
        second = generate_project_voice_asset(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectVoiceGenerationSpec(
                text="雅酿酒业，敬每一份努力。",
                voice="narrator-b",
                parent_artifact_id=first.artifact.id,
            ),
        )
        session.commit()

        assert first.artifact.version == 1
        assert second.artifact.version == 2
        assert second.artifact.parent_id == first.artifact.id
        assert first.project_asset.role == "voice"
        assert store.exists(first.artifact.storage_key)
        content = load(first.artifact.content_json)
        assert content["text"] == "雅酿酒业，敬每一份努力。"
        assert content["voice"] == "narrator-a"
        assert content["provenance"]["model"] == "speech-model-v2"
        assert content["provenance"]["cost_status"] == "reported"
        assert content["provenance"]["provider_metadata"]["api_token"] == "[REDACTED]"
        assert load(first.project_asset.metadata_json)["provenance"]["cost"] == {
            "amount": "0.01",
            "currency": "CNY",
        }
        assert provider.requests[0].user_id == "owner-a"


def test_project_tts_probes_duration_and_persists_generation_speed(
    generation_database,
):
    class MissingDurationSpeechProvider(FakeSpeechProvider):
        def synthesize(self, request: SpeechSynthesisRequest):
            generated = super().synthesize(request)
            return replace(generated, duration_seconds=None, word_timings=())

    session_factory, store, task_id, project_id = generation_database
    provider = MissingDurationSpeechProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        generated = generate_project_voice_asset(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectVoiceGenerationSpec(
                text="实际音频时长",
                speed=1.5,
                volume=0.8,
                audio_format="wav",
            ),
        )
        session.commit()

        artifact_content = load(generated.artifact.content_json)
        asset_metadata = load(generated.project_asset.metadata_json)
        assert artifact_content["duration_seconds"] == pytest.approx(0.01)
        assert artifact_content["provider_duration_seconds"] is None
        assert artifact_content["probe_status"] == "succeeded"
        assert asset_metadata["duration_seconds"] == pytest.approx(0.01)
        assert asset_metadata["speed"] == 1.5
        assert asset_metadata["volume"] == 0.8
        assert asset_metadata["probe"]["duration_seconds"] == pytest.approx(0.01)


def test_ai_logo_candidates_are_never_marked_as_confirmed_brand_assets(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeImageProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        result = generate_project_image_candidates(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectImageGenerationSpec(
                prompt="生成用于构图讨论的圆形绿色 Logo 草案",
                role="logo",
                count=2,
                label="Logo 草案",
            ),
        )
        session.commit()

        assert len(result.candidates) == 2
        assert [item.artifact.version for item in result.candidates] == [1, 2]
        for candidate in result.candidates:
            artifact_content = load(candidate.artifact.content_json)
            asset_metadata = load(candidate.project_asset.metadata_json)
            assert candidate.project_asset.role == "logo"
            assert asset_metadata["pending_brand_confirmation"] is True
            assert asset_metadata["brand_asset_status"] == "pending_confirmation"
            assert asset_metadata["official_brand_asset"] is False
            assert artifact_content["pending_brand_confirmation"] is True
            assert (
                artifact_content["provenance"]["provider_metadata"]["authorization"] == "[REDACTED]"
            )
            assert store.exists(candidate.artifact.storage_key)


def test_project_receives_all_candidates_from_hiagent_single_image_responses(
    generation_database, monkeypatch,
):
    class SingleImageGateway:
        workspace_id = "test-workspace"

        def __init__(self):
            self.calls = []

        def _request(self, action, body):
            self.calls.append((action, body))
            return {"Data": [{"Url": f"https://images.test/{len(self.calls)}.png"}]}

    gateway = SingleImageGateway()
    provider = HiAgentImageGenerationPort(
        client=gateway, model_service_id="image-model", max_candidates=8,
    )
    monkeypatch.setattr(httpx, "get", lambda url, **_kwargs: httpx.Response(
        200, content=PNG + url.encode(), request=httpx.Request("GET", url),
    ))
    session_factory, store, task_id, project_id = generation_database
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        result = generate_project_image_candidates(
            session, project=project, task=task, created_by="owner-a",
            provider=provider, asset_store=store,
            spec=ProjectImageGenerationSpec(prompt="公司办公场景", role="video", count=2),
        )
        session.commit()
        assert len(result.candidates) == 2
        assert len(gateway.calls) == 2
        assert len({store.read_bytes(item.artifact.storage_key) for item in result.candidates}) == 2
        assert [load(item.artifact.content_json)["candidate_index"] for item in result.candidates] == [0, 1]


def test_invalid_generated_image_is_rejected_before_storage_or_database_rows(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeImageProvider(corrupt=True)
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        with pytest.raises(VideoProjectGenerationValidationError, match="文件内容"):
            generate_project_image_candidates(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=ProjectImageGenerationSpec(prompt="测试错误返回"),
            )
        assert session.scalar(select(Artifact).where(Artifact.task_id == task_id)) is None
        assert (
            session.scalar(select(ProjectAsset).where(ProjectAsset.project_id == project_id))
            is None
        )
        assert not list(store.root.rglob("*"))


def test_project_image_references_are_deduplicated_by_checksum_before_limit(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeImageProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        generate_project_image_candidates(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectImageGenerationSpec(
                prompt="生成品牌画面",
                reference_images=(PNG,) * 10,
                reference_media_types=("image/png",) * 10,
            ),
        )

        assert len(provider.requests) == 1
        assert provider.requests[0].reference_images == (PNG,)
        assert provider.requests[0].reference_media_types == ("image/png",)


def test_ai_video_enqueue_is_idempotent_and_archive_does_not_poll_provider(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    image_provider = FakeImageProvider()
    video_provider = FakeAiVideoProvider()
    transfer_count = 0

    def transfer(provider_task, *, asset_store, storage_key):
        nonlocal transfer_count
        transfer_count += 1
        assert provider_task.status == "succeeded"
        return asset_store.put_bytes(storage_key, MP4, "video/mp4")

    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        source_image = generate_project_image_candidates(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=image_provider,
            asset_store=store,
            spec=ProjectImageGenerationSpec(prompt="清溪河谷远景"),
        ).candidates[0]
        spec = ProjectAiVideoGenerationSpec(
            prompt="镜头缓慢推进，酒瓶保持稳定",
            source_copy_artifact_id="confirmed-copy-1",
            reference_images=(PNG + b"-product-reference",),
            reference_media_types=("image/png",),
            source_project_asset_id=source_image.project_asset.id,
            knowledge_resource_ids=("product-knowledge",),
            visual_reference_resource_ids=("product-visual",),
            duration_seconds=5,
            ratio="16:9",
            idempotency_key="project-video-shot-1",
        )
        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=video_provider,
            asset_store=store,
            spec=spec,
        )
        repeated = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=video_provider,
            asset_store=store,
            spec=spec,
        )
        assert enqueued.created is True
        assert repeated.created is False
        assert repeated.job.id == enqueued.job.id
        assert len(video_provider.requests) == 1
        with pytest.raises(VideoProjectGenerationConflictError, match="幂等键"):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=video_provider,
                asset_store=store,
                spec=replace(spec, source_copy_artifact_id="confirmed-copy-2"),
            )
        assert video_provider.requests[0].image_url.startswith("data:image/png;base64,")
        assert len(video_provider.requests[0].reference_image_urls) == 1

        lease = claim_job(
            session,
            worker_id="video-worker",
            job_id=enqueued.job.id,
        )
        assert lease is not None
        completed_provider_task = AiVideoTask(
            provider_task_id=enqueued.provider_task_id,
            status="succeeded",
            provider_id=video_provider.status.provider_id,
            model=video_provider.status.model or "",
            progress=100,
            video_url="https://cdn.example/generated.mp4",
            metadata={
                "usage": {"generated_seconds": 5},
                "cost": {"amount": "1.23", "currency": "CNY"},
            },
        )
        archived = archive_project_ai_video_result(
            session,
            project=project,
            task=task,
            job=enqueued.job,
            provider_task=completed_provider_task,
            asset_store=store,
            created_by="owner-a",
            video_transfer=transfer,
        )
        assert session.scalar(select(Job.status).where(Job.id == enqueued.job.id)) == "running"
        transition = complete_job(
            session,
            lease,
            result_json=archived.job_result,
        )
        assert transition is not None and transition.status == "succeeded"
        session.commit()

        assert archived.generated.project_asset.role == "video"
        assert archived.generated.artifact.storage_key
        assert store.exists(archived.generated.artifact.storage_key)
        assert "storage_key" not in archived.job_result
        assert archived.job_result["project_asset_id"] == (archived.generated.project_asset.id)
        artifact_content = load(archived.generated.artifact.content_json)
        project_asset_metadata = load(archived.generated.project_asset.metadata_json)
        assert artifact_content["knowledge_resource_ids"] == ["product-knowledge"]
        assert artifact_content["visual_reference_resource_ids"] == ["product-visual"]
        assert artifact_content["provenance"]["request"]["visual_reference_resource_ids"] == [
            "product-visual"
        ]
        assert project_asset_metadata["knowledge_resource_ids"] == ["product-knowledge"]
        assert project_asset_metadata["visual_reference_resource_ids"] == ["product-visual"]
        assert project_asset_metadata["duration_seconds"] == 5
        video_stage = session.scalar(
            select(TaskStage).where(
                TaskStage.task_id == task.id,
                TaskStage.stage_key == "video_preview",
            )
        )
        assert load(video_stage.selected_artifact_ids_json)["ai_video_clip"] == [
            archived.generated.artifact.id
        ]
        assert archived.generated.artifact.status == "selected"

    with session_factory() as session:
        job = session.get(Job, enqueued.job.id)
        task, project = context(session, task_id, project_id)
        repeated_archive = archive_project_ai_video_result(
            session,
            project=project,
            task=task,
            job=job,
            provider_task=completed_provider_task,
            asset_store=store,
            created_by="owner-a",
            video_transfer=transfer,
        )
        assert repeated_archive.generated.project_asset.id == (archived.generated.project_asset.id)
        assert transfer_count == 1


def test_ai_video_text_to_video_uses_declared_options_and_allowlisted_task_context(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()

    assert provider.capabilities["text_to_video"] is True
    assert provider.capabilities["durations"] == [5, 10, 15]
    assert provider.capabilities["ratios"] == ["16:9", "9:16", "1:1"]
    assert provider.capabilities["resolutions"] == ["480p", "720p"]

    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        task.product = "臻品雅酿"
        task.audience = "年轻消费者"
        task.requirements = "避免夸大表达"
        task.keywords_json = '["不得外发的关键词"]'
        task.length = "不得外发的篇幅"
        task.allowed_users_json = '["不得外发的用户"]'
        task.allowed_departments_json = '["不得外发的部门"]'
        session.flush()

        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt="镜头从清溪河缓慢推进至酒瓶",
                duration_seconds=10,
                ratio="9:16",
                resolution="480p",
            ),
        )

        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.image_url is None
        assert request.duration_seconds == 10
        assert request.ratio == "9:16"
        assert request.resolution == "480p"
        assert request.prompt == (
            "请结合以下任务简报和用户镜头要求生成视频。\n"
            "任务名称：工程素材生成\n"
            "主题：品牌短视频\n"
            "产品：臻品雅酿\n"
            "目标人群：年轻消费者\n"
            "内容类型：短视频\n"
            "表达风格：品牌宣传型\n"
            "补充要求：避免夸大表达\n"
            "用户镜头要求：镜头从清溪河缓慢推进至酒瓶"
        )
        assert all(
            marker not in request.prompt
            for marker in (
                task.id,
                task.owner_id,
                "不得外发的关键词",
                "不得外发的篇幅",
                "不得外发的用户",
                "不得外发的部门",
            )
        )

        persisted_request = load(enqueued.job.result_json)["request"]
        assert persisted_request["context_included"] is True
        assert persisted_request["task_context"] == {
            "task_name": "工程素材生成",
            "topic": "品牌短视频",
            "product": "臻品雅酿",
            "audience": "年轻消费者",
            "content_type": "短视频",
            "style": "品牌宣传型",
            "requirements": "避免夸大表达",
        }


def test_ai_video_supports_multiple_reference_images_and_videos(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    image_provider = FakeImageProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        images = generate_project_image_candidates(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=image_provider,
            asset_store=store,
            spec=ProjectImageGenerationSpec(prompt="三张参考图", count=3),
        ).candidates
        first_video = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=1,
            duration_seconds=6,
        )
        second_video = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=2,
            duration_seconds=7,
        )

        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt="结合全部参考素材生成视频",
                # The product reference has the same bytes as the manually
                # selected second image and must win that de-duplication slot.
                reference_images=(PNG + bytes([1]),),
                reference_media_types=("image/png",),
                source_project_asset_id=images[0].project_asset.id,
                reference_image_asset_ids=(
                    images[1].project_asset.id,
                    images[2].project_asset.id,
                ),
                reference_video_asset_ids=(first_video.id, second_video.id),
                knowledge_resource_ids=("product-knowledge",),
                visual_reference_resource_ids=("product-image",),
            ),
        )

        request = provider.requests[0]
        assert request.image_url.startswith("data:image/png;base64,")
        assert base64.b64decode(request.image_url.split(",", 1)[1]) == PNG + bytes([0])
        assert len(request.reference_image_urls) == 2
        assert request.reference_image_urls[0].startswith("data:image/png;base64,")
        assert base64.b64decode(request.reference_image_urls[0].split(",", 1)[1]) == (
            PNG + bytes([1])
        )
        assert base64.b64decode(request.reference_image_urls[1].split(",", 1)[1]) == (
            PNG + bytes([2])
        )
        first_artifact = session.get(Artifact, first_video.artifact_id)
        second_artifact = session.get(Artifact, second_video.artifact_id)
        assert first_artifact is not None and first_artifact.storage_key
        assert second_artifact is not None and second_artifact.storage_key
        assert request.reference_video_urls == (
            store.provider_url(first_artifact.storage_key),
            store.provider_url(second_artifact.storage_key),
        )
        assert request.reference_video_durations_seconds == (6, 7)
        persisted = load(enqueued.job.result_json)["request"]
        assert persisted["reference_image_asset_ids"] == [
            images[1].project_asset.id,
            images[2].project_asset.id,
        ]
        assert persisted["reference_video_asset_ids"] == [first_video.id, second_video.id]
        assert persisted["reference_video_durations_seconds"] == [6.0, 7.0]
        assert persisted["knowledge_resource_ids"] == ["product-knowledge"]
        assert persisted["visual_reference_resource_ids"] == ["product-image"]
        assert persisted["effective_reference_image_count"] == 3
        assert persisted["reference_image_manifest"] == [
            {
                "sequence": 1,
                "provider_content_index": 1,
                "source": "source_project_asset",
                "project_asset_id": images[0].project_asset.id,
                "label": images[0].project_asset.label,
            },
            {
                "sequence": 2,
                "provider_content_index": 2,
                "source": "knowledge_visual",
                "resource_entry_id": "product-image",
            },
            {
                "sequence": 3,
                "provider_content_index": 3,
                "source": "project_asset",
                "project_asset_id": images[2].project_asset.id,
                "label": images[2].project_asset.label,
            },
        ]


def test_external_ai_video_prefers_private_tos_publication_and_redacts_signed_url(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    publisher = FakeProviderMediaPublisher()
    store.provider_url = lambda _key: pytest.fail("TOS 配置后不应调用本服务媒体网关")

    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        video = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=11,
            duration_seconds=5,
        )
        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            provider_media_publisher=publisher,
            spec=ProjectAiVideoGenerationSpec(
                prompt="使用私有参考视频",
                reference_video_asset_ids=(video.id,),
            ),
        )

        publication = publisher.published[0]["publication"]
        request = provider.requests[0]
        assert request.reference_video_urls == (publication.url,)
        assert publisher.published[0]["media_type"] == "video/mp4"
        assert publisher.published[0]["source_path"] == store.path_for(
            session.get(Artifact, video.artifact_id).storage_key
        )
        internal = load(enqueued.job.result_json)
        serialized_internal = json.dumps(internal, ensure_ascii=False)
        assert publication.url not in serialized_internal
        assert "X-Tos-Signature" not in serialized_internal
        assert internal["request"]["reference_video_transports"] == [publisher.transport]
        assert internal["provenance"]["request"]["reference_video_transports"] == [
            publisher.transport
        ]
        assert internal["_provider_media_publications"] == [
            {
                "transport": publisher.transport,
                "cleanup_token": publication.cleanup_token,
                "expires_at": publication.expires_at.isoformat(),
            }
        ]

        from content_service.main import job_json

        api_payload = job_json(enqueued.job)
        assert "_provider_media_publications" not in api_payload["result"]
        assert "X-Tos-Signature" not in json.dumps(
            api_payload,
            ensure_ascii=False,
            default=str,
        )

        assert release_provider_media_publications(publisher, internal) == 1
        assert publisher.released == [publication.cleanup_token]


def test_existing_ai_video_reuses_refreshed_provider_output_without_gateway(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = ProviderManagedOutputAiVideoProvider(
        source_video_url="https://ark-output.example/source.mp4?signature=temporary"
    )
    store.provider_url = lambda _key: None

    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        source = create_ai_generated_video_reference_asset(
            session,
            store,
            task,
            project,
        )

        serialized = project_asset_json(session, source)
        assert serialized["metadata"]["provider_native_reference"] == {
            "eligible": True,
            "provider_id": "video-provider",
            "transport": "provider-managed-output-https",
            "reference_kind": "content-generation-task-output",
        }
        assert serialized["metadata"]["duration_seconds"] == 4

        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt="以已有 AI 视频为参考",
                reference_video_asset_ids=(source.id,),
            ),
        )

        assert provider.get_calls == ["source-provider-task-1"]
        assert provider.requests[0].reference_video_urls == (
            "https://ark-output.example/source.mp4?signature=temporary",
        )
        persisted = load(enqueued.job.result_json)
        assert persisted["request"]["reference_video_transports"] == [
            "provider-managed-output-https"
        ]
        assert "ark-output.example" not in json.dumps(persisted, ensure_ascii=False)


@pytest.mark.parametrize(
    ("source_provider_id", "source_status", "message", "expected_get_calls"),
    [
        ("other-video-provider", "succeeded", "其他 Provider", []),
        ("video-provider", "expired", "Provider 输出已不可用", ["source-provider-task-1"]),
    ],
)
def test_provider_native_video_reference_fails_closed_when_not_reusable(
    generation_database,
    source_provider_id,
    source_status,
    message,
    expected_get_calls,
):
    session_factory, store, task_id, project_id = generation_database
    provider = ProviderManagedOutputAiVideoProvider(
        source_status=source_status,
        source_video_url=(
            "https://ark-output.example/source.mp4" if source_status == "succeeded" else None
        ),
    )
    store.provider_url = lambda _key: None

    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        source = create_ai_generated_video_reference_asset(
            session,
            store,
            task,
            project,
            provider_id=source_provider_id,
        )

        with pytest.raises(VideoProjectGenerationValidationError, match=message):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=ProjectAiVideoGenerationSpec(
                    prompt="不可复用时必须关闭",
                    reference_video_asset_ids=(source.id,),
                ),
            )
        assert provider.get_calls == expected_get_calls
        assert provider.requests == []


def test_regular_project_video_cannot_spoof_provider_native_marker(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        source = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=91,
            duration_seconds=5,
        )
        metadata = load(source.metadata_json)
        metadata["provider_native_reference"] = {
            "eligible": True,
            "provider_id": "video-provider",
        }
        source.metadata_json = dump(metadata)
        session.flush()

        assert "provider_native_reference" not in project_asset_json(session, source)["metadata"]


def test_ai_video_submit_failure_releases_private_tos_publication(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    publisher = FakeProviderMediaPublisher()

    def fail_submit(_request):
        raise RuntimeError("provider unavailable")

    provider.submit = fail_submit
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        video = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=12,
            duration_seconds=5,
        )
        with pytest.raises(
            ProviderCallError,
            match="AI 服务暂时无法正常处理请求",
        ) as caught:
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                provider_media_publisher=publisher,
                spec=ProjectAiVideoGenerationSpec(
                    prompt="提交失败也要清理",
                    reference_video_asset_ids=(video.id,),
                ),
            )
        assert "provider unavailable" not in str(caught.value)
    assert publisher.released == [publisher.published[0]["publication"].cleanup_token]


def test_ai_video_retry_style_reenqueue_creates_a_fresh_tos_publication(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    publisher = FakeProviderMediaPublisher()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        video = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=13,
            duration_seconds=5,
        )
        spec = ProjectAiVideoGenerationSpec(
            prompt="重试需要重新发布",
            reference_video_asset_ids=(video.id,),
        )
        first = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            provider_media_publisher=publisher,
            spec=spec,
        )
        second = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            provider_media_publisher=publisher,
            spec=spec,
        )

    assert first.job.id != second.job.id
    assert len(publisher.published) == 2
    assert publisher.published[0]["publication_id"] != publisher.published[1]["publication_id"]
    assert publisher.published[0]["publication"].url != publisher.published[1]["publication"].url


def test_ai_video_product_and_manual_images_share_nine_reference_limit(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    image_provider = FakeImageProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        manual = generate_project_image_candidates(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=image_provider,
            asset_store=store,
            spec=ProjectImageGenerationSpec(prompt="手动参考图"),
        ).candidates[0]
        product_images = tuple(PNG + f"-product-{index}".encode() for index in range(9))

        with pytest.raises(VideoProjectGenerationValidationError, match="最多允许 9 张"):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=ProjectAiVideoGenerationSpec(
                    prompt="生成产品视频",
                    reference_images=product_images,
                    reference_media_types=("image/png",) * len(product_images),
                    reference_image_asset_ids=(manual.project_asset.id,),
                ),
            )
        assert provider.requests == []


def test_ai_video_rejects_reference_video_total_duration_before_submission(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        videos = tuple(
            create_video_reference_asset(
                session,
                store,
                task,
                project,
                index=index,
                duration_seconds=8,
            )
            for index in (1, 2)
        )

        with pytest.raises(VideoProjectGenerationValidationError, match="总时长"):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=ProjectAiVideoGenerationSpec(
                    prompt="超长参考视频",
                    reference_video_asset_ids=tuple(item.id for item in videos),
                ),
            )
        assert provider.requests == []


def test_external_ai_video_rejects_local_video_without_provider_https_url(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = ProviderManagedOutputAiVideoProvider()
    store.provider_url = lambda _key: None
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        video = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=1,
            duration_seconds=5,
        )

        with pytest.raises(VideoProjectGenerationValidationError, match="签名 HTTPS 媒体网关"):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=ProjectAiVideoGenerationSpec(
                    prompt="参考本地视频",
                    reference_video_asset_ids=(video.id,),
                ),
            )
        assert provider.requests == []
        assert provider.get_calls == []


def test_external_ai_video_uploads_local_video_when_provider_accepts_runtime_upload(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database

    class LocalUploadAiVideoProvider(FakeAiVideoProvider):
        capabilities = {
            **FakeAiVideoProvider.capabilities,
            "reference_videos": {
                **FakeAiVideoProvider.capabilities["reference_videos"],
                "local_file_upload": True,
            },
        }

    provider = LocalUploadAiVideoProvider()
    store.provider_url = lambda _key: None
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        video = create_video_reference_asset(
            session,
            store,
            task,
            project,
            index=1,
            duration_seconds=5,
        )
        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt="参考本地视频",
                reference_video_asset_ids=(video.id,),
            ),
        )
        request = provider.requests[0]
        artifact = session.get(Artifact, video.artifact_id)
        assert artifact is not None
        expected = store.path_for(artifact.storage_key).resolve().as_uri()
        assert request.reference_video_urls == (expected,)
        persisted = load(enqueued.job.result_json)
        assert persisted["request"]["reference_video_transports"] == ["hiagent-runtime-upload"]


def test_ai_video_prefers_native_https_over_local_reupload_when_both_available(
    generation_database,
):
    class LocalUploadAndNativeAiVideoProvider(ProviderManagedOutputAiVideoProvider):
        capabilities = {
            **ProviderManagedOutputAiVideoProvider.capabilities,
            "reference_videos": {
                **ProviderManagedOutputAiVideoProvider.capabilities["reference_videos"],
                "local_file_upload": True,
            },
        }

    session_factory, store, task_id, project_id = generation_database
    provider = LocalUploadAndNativeAiVideoProvider(
        source_video_url="https://ark-output.example/source.mp4?signature=temporary"
    )
    store.provider_url = lambda _key: None
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        source = create_ai_generated_video_reference_asset(
            session,
            store,
            task,
            project,
        )
        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt="优先复用已有成片地址",
                reference_video_asset_ids=(source.id,),
            ),
        )
        assert provider.get_calls == ["source-provider-task-1"]
        assert provider.requests[0].reference_video_urls == (
            "https://ark-output.example/source.mp4?signature=temporary",
        )
        persisted = load(enqueued.job.result_json)
        assert persisted["request"]["reference_video_transports"] == [
            "provider-managed-output-https"
        ]


def test_ai_video_falls_back_to_local_upload_when_native_output_expired(
    generation_database,
):
    class LocalUploadAndNativeAiVideoProvider(ProviderManagedOutputAiVideoProvider):
        capabilities = {
            **ProviderManagedOutputAiVideoProvider.capabilities,
            "reference_videos": {
                **ProviderManagedOutputAiVideoProvider.capabilities["reference_videos"],
                "local_file_upload": True,
            },
        }

    session_factory, store, task_id, project_id = generation_database
    provider = LocalUploadAndNativeAiVideoProvider(source_status="expired", source_video_url=None)
    store.provider_url = lambda _key: None
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        source = create_ai_generated_video_reference_asset(
            session,
            store,
            task,
            project,
        )
        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(
                prompt="原生地址失效时改本地上传",
                reference_video_asset_ids=(source.id,),
            ),
        )
        artifact = session.get(Artifact, source.artifact_id)
        assert artifact is not None
        expected = store.path_for(artifact.storage_key).resolve().as_uri()
        assert provider.requests[0].reference_video_urls == (expected,)
        persisted = load(enqueued.job.result_json)
        assert persisted["request"]["reference_video_transports"] == ["hiagent-runtime-upload"]


@pytest.mark.parametrize(
    ("unsupported", "message"),
    [
        ({"duration_seconds": 3}, "至少为 5 秒"),
        ({"duration_seconds": 4.5}, "至少为 5 秒"),
        ({"ratio": "4:3"}, "不支持画面比例"),
        ({"resolution": "1080p"}, "不支持清晰度"),
    ],
)
def test_ai_video_rejects_unsupported_options_before_provider_submission(
    generation_database,
    unsupported,
    message,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()

    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        with pytest.raises(VideoProjectGenerationValidationError, match=message):
            enqueue_project_ai_video(
                session,
                project=project,
                task=task,
                created_by="owner-a",
                provider=provider,
                asset_store=store,
                spec=ProjectAiVideoGenerationSpec(
                    prompt="生成一段品牌视频",
                    **unsupported,
                ),
            )

        assert provider.requests == []
        assert session.scalars(select(Job).where(Job.task_id == task_id)).all() == []


def test_ai_video_archive_rejects_mismatched_provider_task(
    generation_database,
):
    session_factory, store, task_id, project_id = generation_database
    provider = FakeAiVideoProvider()
    with session_factory() as session:
        task, project = context(session, task_id, project_id)
        enqueued = enqueue_project_ai_video(
            session,
            project=project,
            task=task,
            created_by="owner-a",
            provider=provider,
            asset_store=store,
            spec=ProjectAiVideoGenerationSpec(prompt="生成一段品牌视频"),
        )
        wrong = AiVideoTask(
            provider_task_id="another-provider-task",
            status="succeeded",
            provider_id=provider.status.provider_id,
            model=provider.status.model or "",
            video_url="https://cdn.example/generated.mp4",
        )
        with pytest.raises(VideoProjectGenerationConflictError, match="任务 ID"):
            archive_project_ai_video_result(
                session,
                project=project,
                task=task,
                job=enqueued.job,
                provider_task=wrong,
                asset_store=store,
                created_by="owner-a",
            )


def test_maybe_retry_ai_video_on_fast_retries_invalid_params():
    class _RetryProvider:
        def submit_fast_retry(self, payload):
            self.payload = payload
            return AiVideoTask(
                provider_task_id="fast-task",
                status="queued",
                provider_id="hiagent-video",
                model="fast-model",
            )

    provider = _RetryProvider()
    failed = AiVideoTask(
        provider_task_id="pro-task",
        status="failed",
        provider_id="hiagent-video",
        model="pro-model",
        error_code="invalid_parameters",
        error_message="提交的生成参数不符合当前 AI 模型要求",
    )
    retried = maybe_retry_ai_video_on_fast(
        provider,
        failed=failed,
        metadata={
            "retry_payload": {
                "Prompt": "根据参考图生成视频",
                "Model": "pro-model",
                "Files": [{"Role": "reference_image"}],
            }
        },
    )
    assert retried is not None
    assert retried.provider_task_id == "fast-task"
    assert provider.payload["Files"][0]["Role"] == "reference_image"


def test_maybe_retry_ai_video_on_fast_skips_content_safety():
    class _RetryProvider:
        def submit_fast_retry(self, payload):
            raise AssertionError("安全拦截不应改走 Fast")

    failed = AiVideoTask(
        provider_task_id="pro-task",
        status="failed",
        provider_id="hiagent-video",
        model="pro-model",
        error_code="content_safety",
        error_message="InputImageSensitiveContentDetected",
    )
    assert (
        maybe_retry_ai_video_on_fast(
            _RetryProvider(),
            failed=failed,
            metadata={"retry_payload": {"Prompt": "根据参考视频生成", "Model": "pro-model"}},
        )
        is None
    )
