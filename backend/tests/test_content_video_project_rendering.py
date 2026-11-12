import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
CONTENT_SRC = ROOT / "services" / "content_service" / "src"
if str(CONTENT_SRC) not in sys.path:
    sys.path.insert(0, str(CONTENT_SRC))

from content_service.db import Base
from content_service.media_adapters import LocalAssetStore, MockSpeechSynthesisPort
from content_service.models import (
    Artifact,
    AssetRef,
    LibraryItem,
    ProjectAsset,
    ResourceEntry,
    VideoProject,
    VideoProjectRevision,
)
from content_service.providers import SpeechSynthesisRequest
from content_service.timeline_media import FfmpegTimelineRenderer
from content_service.video_project_rendering import (
    ProjectAssetResolver,
    ProjectBundleExportOptions,
    VideoProjectRenderingError,
    build_stem_export_plan,
    export_video_project_bundle,
    prepare_video_project_render,
    render_stem_export,
    select_direct_stem_source,
)


def _id() -> str:
    return str(uuid4())


def _image_bytes(color: str, *, size: tuple[int, int] = (96, 64)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _wav_bytes() -> bytes:
    return (
        MockSpeechSynthesisPort()
        .synthesize(SpeechSynthesisRequest("video project rendering test", audio_format="wav"))
        .audio
    )


def _track(kind: str, items: list[dict]) -> dict:
    return {
        "id": f"track-{kind}",
        "kind": kind,
        "name": kind,
        "muted": False,
        "locked": False,
        "items": items,
    }


def _item(
    item_id: str,
    project_asset_id: str | None,
    *,
    start: float,
    duration: float,
    content: str = "",
    volume: float = 1,
    metadata: dict | None = None,
    transform: dict | None = None,
) -> dict:
    return {
        "id": item_id,
        "project_asset_id": project_asset_id,
        "label": item_id,
        "start_seconds": start,
        "duration_seconds": duration,
        "trim_in_seconds": 0,
        "trim_out_seconds": 0,
        "volume": volume,
        "speed": 1,
        "enabled": True,
        "content": content,
        "transform": transform or {"raw_filter": "movie=/etc/passwd"},
        "metadata": metadata or {},
    }


@dataclass
class ProjectFixture:
    session: Session
    store: LocalAssetStore
    project: VideoProject
    revision_one: VideoProjectRevision
    revision_two: VideoProjectRevision
    assets: dict[str, ProjectAsset]
    object_root: Path
    work_root: Path
    output_root: Path


def _asset_ref_source(
    fixture_session: Session,
    store: LocalAssetStore,
    *,
    key: str,
    content: bytes,
    media_type: str,
    resource_kind: str,
    duration: float | None = None,
    audio_codec: str | None = None,
) -> tuple[ResourceEntry, AssetRef]:
    stored = store.put_bytes(key, content, media_type)
    entry = ResourceEntry(
        id=_id(),
        owner_id="user-a",
        resource_kind=resource_kind,
        title=Path(key).name,
        active=True,
    )
    metadata = {
        "probe": {
            "duration_seconds": duration,
            "audio_codec": audio_codec,
        }
    }
    asset_ref = AssetRef(
        id=_id(),
        entry_id=entry.id,
        storage_key=stored.key,
        media_type=media_type,
        size_bytes=stored.size,
        checksum=stored.checksum_sha256,
        metadata_json=json.dumps(metadata),
    )
    fixture_session.add_all([entry, asset_ref])
    fixture_session.flush()
    return entry, asset_ref


def _project_asset(
    session: Session,
    project_id: str,
    source,
    *,
    role: str,
    label: str,
    source_type: str,
    metadata: dict | None = None,
) -> ProjectAsset:
    source_field = {
        "asset_ref": "asset_ref_id",
        "artifact": "artifact_id",
        "library_item": "library_item_id",
        "resource_entry": "resource_entry_id",
    }[source_type]
    item = ProjectAsset(
        id=_id(),
        project_id=project_id,
        source_type=source_type,
        source_id=source.id,
        role=role,
        label=label,
        metadata_json=json.dumps(metadata or {}),
        created_by="user-a",
        **{source_field: source.id},
    )
    session.add(item)
    session.flush()
    return item


@pytest.fixture
def project_fixture(tmp_path) -> ProjectFixture:
    engine = create_engine(f"sqlite:///{tmp_path / 'project-rendering.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    object_root = tmp_path / "objects"
    work_root = object_root / "render-work"
    output_root = tmp_path / "exports"
    store = LocalAssetStore(object_root)

    project_id = _id()
    revision_one_id = _id()
    revision_two_id = _id()
    project = VideoProject(
        id=project_id,
        task_id=_id(),
        current_revision=2,
        current_revision_id=revision_two_id,
        created_by="user-a",
    )
    session.add(project)
    session.flush()

    _, intro_ref = _asset_ref_source(
        session,
        store,
        key="images/intro.png",
        content=_image_bytes("#9b1c31"),
        media_type="image/png",
        resource_kind="image",
    )
    _, main_ref = _asset_ref_source(
        session,
        store,
        key="images/main.png",
        content=_image_bytes("#176b5f"),
        media_type="image/png",
        resource_kind="image",
    )
    outro_entry, _ = _asset_ref_source(
        session,
        store,
        key="images/outro.png",
        content=_image_bytes("#c78f2d"),
        media_type="image/png",
        resource_kind="image",
    )
    _, logo_ref = _asset_ref_source(
        session,
        store,
        key="images/logo.png",
        content=_image_bytes("#ffffff", size=(32, 16)),
        media_type="image/png",
        resource_kind="image",
    )
    subtitle_entry, _ = _asset_ref_source(
        session,
        store,
        key="subtitles/provided.vtt",
        content=b"WEBVTT\n\n00:00.050 --> 00:00.350\nProvided subtitle\n",
        media_type="text/vtt",
        resource_kind="subtitle",
    )

    voice_stored = store.put_bytes("audio/voice.wav", _wav_bytes(), "audio/wav")
    voice_artifact = Artifact(
        id=_id(),
        task_id=project.task_id,
        kind="voice",
        status="confirmed",
        content_json=json.dumps({"media_type": "audio/wav", "duration_seconds": 1.2}),
        storage_key=voice_stored.key,
        created_by="user-a",
    )
    session.add(voice_artifact)
    session.flush()

    _, music_ref = _asset_ref_source(
        session,
        store,
        key="audio/music.wav",
        content=_wav_bytes(),
        media_type="audio/wav",
        resource_kind="audio",
        duration=1.2,
        audio_codec="pcm_s16le",
    )
    music_library = LibraryItem(
        id=_id(),
        owner_id="user-a",
        category="music",
        name="Brand music",
        storage_key=music_ref.storage_key,
        active=True,
    )
    session.add(music_library)
    session.flush()

    sfx_entry, _ = _asset_ref_source(
        session,
        store,
        key="audio/sfx.wav",
        content=_wav_bytes(),
        media_type="audio/wav",
        resource_kind="audio",
        duration=1.2,
        audio_codec="pcm_s16le",
    )

    assets = {
        "intro": _project_asset(
            session,
            project.id,
            intro_ref,
            role="intro",
            label="片头",
            source_type="asset_ref",
        ),
        "main": _project_asset(
            session,
            project.id,
            main_ref,
            role="video",
            label="主画面",
            source_type="asset_ref",
        ),
        "outro": _project_asset(
            session,
            project.id,
            outro_entry,
            role="outro",
            label="片尾",
            source_type="resource_entry",
        ),
        "logo": _project_asset(
            session,
            project.id,
            logo_ref,
            role="logo",
            label="Logo",
            source_type="asset_ref",
        ),
        "subtitle": _project_asset(
            session,
            project.id,
            subtitle_entry,
            role="subtitle",
            label="外部字幕",
            source_type="resource_entry",
        ),
        "voice": _project_asset(
            session,
            project.id,
            voice_artifact,
            role="voice",
            label="旁白",
            source_type="artifact",
        ),
        "music": _project_asset(
            session,
            project.id,
            music_library,
            role="music",
            label="背景音乐",
            source_type="library_item",
        ),
        "sfx": _project_asset(
            session,
            project.id,
            sfx_entry,
            role="sfx",
            label="音效",
            source_type="resource_entry",
        ),
    }
    timeline = {
        "canvas": {"width": 96, "height": 64, "fps": 20, "aspect_ratio": "3:2"},
        "tracks": [
            _track(
                "video",
                [
                    _item(
                        "clip-intro",
                        assets["intro"].id,
                        start=0,
                        duration=0.4,
                        metadata={"legacy_role": "intro", "source_audio": "keep"},
                    ),
                    _item(
                        "clip-main",
                        assets["main"].id,
                        start=0.4,
                        duration=0.5,
                        metadata={
                            "transition": "fade",
                            "transition_duration_seconds": 0.1,
                            "raw_filter": "movie=/etc/passwd",
                            "path": "C:/Windows/System32/config/SAM",
                        },
                    ),
                    _item(
                        "clip-outro",
                        assets["outro"].id,
                        start=0.9,
                        duration=0.4,
                        metadata={"legacy_role": "outro"},
                    ),
                ],
            ),
            _track(
                "overlay",
                [
                    _item(
                        "logo-item",
                        assets["logo"].id,
                        start=0.1,
                        duration=0.8,
                        metadata={"position": "bottom_right", "width": 32, "opacity": 0.7},
                        transform={
                            "x": 25,
                            "y": 75,
                            "scale": 1.5,
                            "opacity": 0.4,
                            "raw_filter": "movie=/etc/passwd",
                        },
                    )
                ],
            ),
            _track(
                "subtitle",
                [
                    _item(
                        "subtitle-file",
                        assets["subtitle"].id,
                        start=0,
                        duration=1.2,
                    ),
                    _item(
                        "subtitle-inline",
                        None,
                        start=0.4,
                        duration=0.45,
                        content="雅酿智能创作",
                        metadata={"subtitle_style": {"font_size": 24, "bold": True}},
                    ),
                ],
            ),
            _track(
                "voice",
                [
                    _item(
                        "voice-item",
                        assets["voice"].id,
                        start=0.1,
                        duration=0.7,
                        volume=0.9,
                        metadata={"fade_in_seconds": 0.05, "fade_out_seconds": 0.05},
                    )
                ],
            ),
            _track(
                "music",
                [
                    _item(
                        "music-item",
                        assets["music"].id,
                        start=0,
                        duration=1.2,
                        volume=0.2,
                        metadata={"loop": True, "fade_out_seconds": 0.1},
                    )
                ],
            ),
            _track(
                "sfx",
                [
                    _item(
                        "sfx-item",
                        assets["sfx"].id,
                        start=0.55,
                        duration=0.25,
                        volume=0.6,
                    )
                ],
            ),
        ],
    }
    revision_one = VideoProjectRevision(
        id=revision_one_id,
        project_id=project.id,
        revision=1,
        source="human",
        timeline_json=json.dumps(timeline, ensure_ascii=False),
        operations_json="[]",
        created_by="user-a",
    )
    revision_two = VideoProjectRevision(
        id=revision_two_id,
        project_id=project.id,
        revision=2,
        parent_revision_id=revision_one.id,
        source="human",
        timeline_json=json.dumps(timeline, ensure_ascii=False),
        operations_json="[]",
        created_by="user-a",
    )
    session.add_all([revision_one, revision_two])
    session.commit()

    fixture = ProjectFixture(
        session=session,
        store=store,
        project=project,
        revision_one=revision_one,
        revision_two=revision_two,
        assets=assets,
        object_root=object_root,
        work_root=work_root,
        output_root=output_root,
    )
    yield fixture
    session.close()
    engine.dispose()


def test_compile_current_and_pinned_revision_maps_all_supported_tracks(project_fixture):
    fixture = project_fixture
    prepared = prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "current.mp4",
        work_root=fixture.work_root,
    )
    assert prepared.revision_id == fixture.revision_two.id
    assert [item.item_id for item in prepared.item_sources if item.track_kind == "video"] == [
        "clip-intro",
        "clip-main",
        "clip-outro",
    ]
    assert len(prepared.request.clips) == 3
    assert prepared.request.clips[0].source_audio == "mute"
    assert prepared.request.clips[1].transition == "fade"
    assert [track.kind for track in prepared.request.audio_tracks] == [
        "voice",
        "music",
        "sfx",
    ]
    assert prepared.request.audio_tracks[1].loop is True
    assert prepared.request.ducking.enabled is True
    assert prepared.request.logos[0].position == "bottom_right"
    assert prepared.request.logos[0].width == 48
    assert prepared.request.logos[0].opacity == 0.4
    assert prepared.request.logos[0].x_percent == 25
    assert prepared.request.logos[0].y_percent == 75
    assert len(prepared.request.subtitles) == 2
    inline_path = next(
        subtitle.source_path
        for subtitle in prepared.request.subtitles
        if subtitle.asset_id.startswith("inline-subtitle")
    )
    assert "雅酿智能创作" in inline_path.read_text(encoding="utf-8")
    assert "movie=/etc/passwd" not in repr(prepared.request)
    assert "System32" not in repr(prepared.request)
    prepared.cleanup()
    assert not inline_path.exists()

    # Archiving only removes an asset from the active library. Immutable
    # revisions must remain reproducible after archive/restore operations.
    fixture.assets["intro"].active = False
    fixture.session.flush()
    pinned = prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "pinned.mp4",
        work_root=fixture.work_root,
        revision_id=fixture.revision_one.id,
    )
    assert pinned.revision_id == fixture.revision_one.id
    assert pinned.revision == 1
    pinned.cleanup()


def test_compile_rejects_cross_project_revision_and_inconsistent_source(project_fixture):
    fixture = project_fixture
    other_project = VideoProject(
        id=_id(),
        task_id=_id(),
        current_revision=1,
        current_revision_id=_id(),
        created_by="user-b",
    )
    other_revision = VideoProjectRevision(
        id=other_project.current_revision_id,
        project_id=other_project.id,
        revision=1,
        timeline_json=fixture.revision_one.timeline_json,
        created_by="user-b",
    )
    fixture.session.add_all([other_project, other_revision])
    fixture.session.commit()

    with pytest.raises(VideoProjectRenderingError, match="已提交版本"):
        prepare_video_project_render(
            fixture.session,
            fixture.project,
            fixture.store,
            fixture.output_root / "bad.mp4",
            work_root=fixture.work_root,
            revision_id=other_revision.id,
        )

    fixture.assets["intro"].source_id = _id()
    fixture.session.flush()
    resolver = ProjectAssetResolver(fixture.session, fixture.project, fixture.store)
    with pytest.raises(VideoProjectRenderingError, match="来源标识"):
        resolver.resolve(fixture.assets["intro"].id)


def _legacy_subtitle_revision(fixture, *, extension="srt", original_text=""):
    payload = (
        "1\n00:00:00,050 --> 00:00:00,350\n真实文件字幕\n"
        if extension == "srt"
        else "WEBVTT\n\n00:00.050 --> 00:00.350\n真实文件字幕\n"
    )
    entry, _ = _asset_ref_source(
        fixture.session,
        fixture.store,
        key=f"subtitles/legacy.{extension}",
        content=payload.encode("utf-8"),
        media_type="application/x-subrip" if extension == "srt" else "text/vtt",
        resource_kind="subtitle",
    )
    asset = _project_asset(
        fixture.session,
        fixture.project.id,
        entry,
        role="subtitle",
        label=f"旧素材标题.{extension}",
        source_type="resource_entry",
        metadata={"text": original_text} if original_text else {},
    )
    timeline = json.loads(fixture.revision_one.timeline_json)
    item = next(track["items"][0] for track in timeline["tracks"] if track["kind"] == "subtitle")
    item["project_asset_id"] = asset.id
    item["label"] = asset.label
    item["content"] = original_text or asset.label
    item["metadata"] = {
        "asset_role": "subtitle",
        "media_kind": "file",
        "resource_kind": "file",
        **({"text": original_text} if original_text else {}),
    }
    return timeline, item, asset


@pytest.mark.parametrize(
    ("extension", "original_text", "media_kind"),
    [("srt", "", "file"), ("vtt", "旧素材文本", "subtitle")],
)
def test_legacy_subtitle_placeholder_uses_file_without_changing_revision(
    project_fixture, extension, original_text, media_kind
):
    fixture = project_fixture
    timeline, item, asset = _legacy_subtitle_revision(
        fixture, extension=extension, original_text=original_text
    )
    item["metadata"].update(media_kind=media_kind, resource_kind=media_kind)
    original_timeline = json.dumps(timeline, ensure_ascii=False)
    fixture.revision_one.timeline_json = original_timeline
    fixture.session.flush()

    with prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "legacy-subtitle.mp4",
        work_root=fixture.work_root,
        revision_id=fixture.revision_one.id,
    ) as prepared:
        subtitle = next(
            subtitle for subtitle in prepared.request.subtitles if subtitle.asset_id == asset.id
        )
        assert "真实文件字幕" in subtitle.source_path.read_text(encoding="utf-8")
        assert item["content"] not in subtitle.source_path.read_text(encoding="utf-8")
        assert prepared.revision_id == fixture.revision_one.id
        assert fixture.revision_one.timeline_json == original_timeline
        assert fixture.project.current_revision_id == fixture.revision_two.id


@pytest.mark.parametrize(
    "conflict", ["edited_text", "missing_shape", "changed_metadata", "wrong_role"]
)
def test_legacy_subtitle_compatibility_preserves_real_conflict_errors(project_fixture, conflict):
    fixture = project_fixture
    timeline, item, asset = _legacy_subtitle_revision(fixture)
    if conflict == "edited_text":
        item["content"] = "用户重新编辑的字幕，必须保留"
    elif conflict == "missing_shape":
        item["metadata"] = {}
    elif conflict == "changed_metadata":
        item["metadata"]["text"] = item["content"] = "与原素材不一致的文本"
    else:
        asset.role = "overlay"
    original_timeline = json.dumps(timeline, ensure_ascii=False)
    fixture.revision_one.timeline_json = original_timeline
    fixture.session.flush()

    with pytest.raises(VideoProjectRenderingError, match="不能同时提供内联文本和素材"):
        prepare_video_project_render(
            fixture.session,
            fixture.project,
            fixture.store,
            fixture.output_root / "subtitle-conflict.mp4",
            work_root=fixture.work_root,
            revision_id=fixture.revision_one.id,
        )
    assert fixture.revision_one.timeline_json == original_timeline


def test_legacy_subtitle_placeholder_does_not_ignore_file_time_offset(project_fixture):
    fixture = project_fixture
    timeline, item, _ = _legacy_subtitle_revision(fixture)
    item["start_seconds"] = 0.25
    fixture.revision_one.timeline_json = json.dumps(timeline, ensure_ascii=False)
    fixture.session.flush()

    with pytest.raises(VideoProjectRenderingError, match="从 0 秒放置"):
        prepare_video_project_render(
            fixture.session,
            fixture.project,
            fixture.store,
            fixture.output_root / "subtitle-offset.mp4",
            work_root=fixture.work_root,
            revision_id=fixture.revision_one.id,
        )


def test_ffmpeg_renders_legacy_subtitle_placeholder_when_available(project_fixture):
    fixture = project_fixture
    timeline, _, _ = _legacy_subtitle_revision(fixture)
    fixture.revision_one.timeline_json = json.dumps(timeline, ensure_ascii=False)
    fixture.session.flush()
    try:
        renderer = FfmpegTimelineRenderer(
            timeout_seconds=60,
            allowed_input_roots=(fixture.object_root,),
            allowed_output_roots=(fixture.output_root,),
        )
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")

    with prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "legacy-subtitle-rendered.mp4",
        work_root=fixture.work_root,
        revision_id=fixture.revision_one.id,
    ) as prepared:
        result = renderer.render(prepared.request)
        assert result.output_path.stat().st_size > 0
        assert result.subtitles_burned == 2


def test_selective_bundle_has_sanitized_json_originals_subtitles_and_checksums(
    project_fixture,
):
    fixture = project_fixture
    output = fixture.output_root / "project-subset.zip"
    result = export_video_project_bundle(
        fixture.session,
        fixture.project,
        fixture.store,
        output,
        allowed_output_roots=(fixture.output_root,),
        revision_id=fixture.revision_one.id,
        options=ProjectBundleExportOptions(
            track_kinds=frozenset({"video", "subtitle", "voice"}),
        ),
    )
    assert result.revision_id == fixture.revision_one.id
    assert result.asset_count == 5
    assert result.inline_subtitle_count == 1
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert {"project.json", "manifest.json", "checksums.sha256"} <= names
        assert "subtitles/inline-subtitles.srt" in names
        assert any(name.startswith("assets/") for name in names)
        assert not any("music" in name.lower() for name in names)
        project_payload = archive.read("project.json").decode("utf-8")
        manifest_payload = archive.read("manifest.json").decode("utf-8")
        assert "storage_key" not in manifest_payload
        assert str(fixture.object_root) not in manifest_payload
        assert "raw_filter" not in project_payload
        assert "System32" not in project_payload
        manifest = json.loads(manifest_payload)
        assert {asset["track_kind"] for asset in manifest["assets"]} == {
            "video",
            "subtitle",
            "voice",
        }
        for line in archive.read("checksums.sha256").decode("utf-8").splitlines():
            expected, archive_path = line.split("  ", 1)
            assert hashlib.sha256(archive.read(archive_path)).hexdigest() == expected


def test_direct_source_and_stem_plan_are_pinned_and_path_allowlisted(project_fixture):
    fixture = project_fixture
    with prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "unused.mp4",
        work_root=fixture.work_root,
        revision_id=fixture.revision_one.id,
    ) as prepared:
        direct = select_direct_stem_source(prepared, "voice")
        assert direct.item_id == "voice-item"
        assert direct.path.is_file()
        assert direct.checksum_sha256 == hashlib.sha256(direct.path.read_bytes()).hexdigest()

        plan = build_stem_export_plan(
            prepared,
            "voice",
            fixture.output_root / "voice-stem.wav",
            allowed_input_roots=(fixture.object_root,),
            allowed_output_roots=(fixture.output_root,),
        )
        command = " ".join(plan.command)
        assert plan.source_count == 1
        assert plan.duration_seconds == pytest.approx(1.2)
        assert "atrim=start=0:duration=0.7" in command
        assert "adelay=100:all=1" in command
        assert "raw_filter" not in command
        assert plan.command[-1] == str((fixture.output_root / "voice-stem.wav").resolve())

        with pytest.raises(VideoProjectRenderingError, match="对象存储目录"):
            build_stem_export_plan(
                prepared,
                "voice",
                fixture.output_root / "outside.wav",
                allowed_input_roots=(fixture.output_root,),
                allowed_output_roots=(fixture.output_root,),
            )


def test_audio_track_speed_compiles_safe_atempo_chain_and_source_span(project_fixture):
    fixture = project_fixture
    timeline = json.loads(fixture.revision_one.timeline_json)
    voice_item = next(track["items"][0] for track in timeline["tracks"] if track["kind"] == "voice")
    voice_item["duration_seconds"] = 0.1
    voice_item["speed"] = 8
    sfx_item = next(track["items"][0] for track in timeline["tracks"] if track["kind"] == "sfx")
    sfx_item["speed"] = 0.1
    fixture.revision_one.timeline_json = json.dumps(timeline)
    fixture.session.flush()

    with prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "speed-preview.mp4",
        work_root=fixture.work_root,
        revision_id=fixture.revision_one.id,
    ) as prepared:
        voice = next(track for track in prepared.request.audio_tracks if track.kind == "voice")
        sfx = next(track for track in prepared.request.audio_tracks if track.kind == "sfx")
        assert voice.duration_seconds == pytest.approx(0.1)
        assert voice.speed == 8
        assert voice.source_out_seconds == pytest.approx(0.8)
        assert sfx.duration_seconds == pytest.approx(0.25)
        assert sfx.speed == pytest.approx(0.1)
        assert sfx.source_out_seconds == pytest.approx(0.025)

        renderer = FfmpegTimelineRenderer(
            allowed_input_roots=(fixture.object_root,),
            allowed_output_roots=(fixture.output_root,),
        )
        command = renderer.build_command(prepared.request)
        graph = command[command.index("-filter_complex") + 1]
        assert "atrim=start=0:duration=0.8" in graph
        assert "atempo=2,atempo=2,atempo=2" in graph
        assert "atrim=start=0:duration=0.025" in graph
        assert "atempo=0.5,atempo=0.5,atempo=0.5,atempo=0.8" in graph

        stem = build_stem_export_plan(
            prepared,
            "voice",
            fixture.output_root / "speed-voice-stem.wav",
            allowed_input_roots=(fixture.object_root,),
            allowed_output_roots=(fixture.output_root,),
        )
        stem_graph = stem.command[stem.command.index("-filter_complex") + 1]
        assert "atrim=start=0:duration=0.8" in stem_graph
        assert "atempo=2,atempo=2,atempo=2" in stem_graph
        assert "atrim=duration=0.1" in stem_graph


def test_video_track_non_unit_speed_remains_rejected(project_fixture):
    fixture = project_fixture
    timeline = json.loads(fixture.revision_one.timeline_json)
    video_track = next(track for track in timeline["tracks"] if track["kind"] == "video")
    video_track["items"][0]["speed"] = 1.25
    fixture.revision_one.timeline_json = json.dumps(timeline)
    fixture.session.flush()

    with pytest.raises(VideoProjectRenderingError, match="视频轨道.*不支持"):
        prepare_video_project_render(
            fixture.session,
            fixture.project,
            fixture.store,
            fixture.output_root / "video-speed.mp4",
            work_root=fixture.work_root,
            revision_id=fixture.revision_one.id,
        )


def test_ffmpeg_project_render_and_stem_integration_when_available(project_fixture):
    fixture = project_fixture
    try:
        renderer = FfmpegTimelineRenderer(
            timeout_seconds=60,
            allowed_input_roots=(fixture.object_root,),
            allowed_output_roots=(fixture.output_root,),
        )
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")

    with prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "project.mp4",
        work_root=fixture.work_root,
        revision_id=fixture.revision_one.id,
    ) as prepared:
        render_result = renderer.render(prepared.request)
        assert render_result.output_path.stat().st_size > 0
        assert render_result.transitions_applied == ("fade", "none")
        assert render_result.logos_applied == 1
        assert render_result.subtitles_burned == 2

        stem_plan = build_stem_export_plan(
            prepared,
            "voice",
            fixture.output_root / "voice-rendered.wav",
            allowed_input_roots=(fixture.object_root,),
            allowed_output_roots=(fixture.output_root,),
            ffmpeg_path=renderer.ffmpeg,
        )
        stem_result = render_stem_export(stem_plan, timeout_seconds=60)
        assert stem_result.output_path.stat().st_size > 44
        assert stem_result.source_count == 1


def test_bundle_and_stem_outputs_require_explicit_controlled_roots(project_fixture):
    fixture = project_fixture
    with pytest.raises(VideoProjectRenderingError, match="受控输出目录"):
        export_video_project_bundle(
            fixture.session,
            fixture.project,
            fixture.store,
            fixture.output_root / "unsafe.zip",
            allowed_output_roots=(),
        )
    prepared = prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "unused.mp4",
        work_root=fixture.work_root,
    )
    with prepared, pytest.raises(VideoProjectRenderingError, match="受控素材目录"):
        build_stem_export_plan(
            prepared,
            "voice",
            fixture.output_root / "unsafe.wav",
            allowed_input_roots=(),
            allowed_output_roots=(fixture.output_root,),
        )


def test_source_audio_stem_command_uses_video_audio_stream(project_fixture):
    fixture = project_fixture
    _, video_ref = _asset_ref_source(
        fixture.session,
        fixture.store,
        key="video/source-with-audio.mp4",
        content=b"trusted-placeholder-for-command-compilation",
        media_type="video/mp4",
        resource_kind="video",
        duration=1,
        audio_codec="aac",
    )
    video_asset = _project_asset(
        fixture.session,
        fixture.project.id,
        video_ref,
        role="video",
        label="带原音视频",
        source_type="asset_ref",
        metadata={"probe": {"duration_seconds": 1, "audio_codec": "aac"}},
    )
    timeline = json.loads(fixture.revision_one.timeline_json)
    video_item = timeline["tracks"][0]["items"][1]
    video_item["project_asset_id"] = video_asset.id
    video_item["metadata"]["source_audio"] = "keep"
    fixture.revision_one.timeline_json = json.dumps(timeline, ensure_ascii=False)
    fixture.session.flush()

    prepared = prepare_video_project_render(
        fixture.session,
        fixture.project,
        fixture.store,
        fixture.output_root / "unused-source.mp4",
        work_root=fixture.work_root,
        revision_id=fixture.revision_one.id,
    )
    with prepared:
        assert prepared.request.clips[1].source_audio == "keep"
        assert prepared.request.clips[1].has_source_audio is True
        direct = select_direct_stem_source(prepared, "source_audio")
        assert direct.item_id == "clip-main"
        assert direct.media_type == "video/mp4"
        plan = build_stem_export_plan(
            prepared,
            "source_audio",
            fixture.output_root / "source.wav",
            allowed_input_roots=(fixture.object_root,),
            allowed_output_roots=(fixture.output_root,),
        )
        graph = plan.command[plan.command.index("-filter_complex") + 1]
        assert plan.source_count == 1
        assert "[0:a]atrim=start=0:duration=0.5" in graph
        assert "adelay=300:all=1" in graph
