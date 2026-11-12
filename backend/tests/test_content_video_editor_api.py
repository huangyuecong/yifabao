from __future__ import annotations

import io
import time
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from PIL import Image


def _png_bytes(color: str = "#176b5f") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (96, 54), color).save(output, "PNG")
    return output.getvalue()


def _mp4_header_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def _create_task(client, headers, *, complete_brief: bool = True) -> dict:
    payload = {
        "mode": "video",
        "name": f"轻量剪辑-{uuid4().hex[:8]}",
        "topic": "清溪河谷品牌短片",
    }
    if complete_brief:
        payload.update(
            {
                "content_type": "短视频口播",
                "style": "品牌宣传型",
                "audience": "年轻消费者",
            }
        )
    response = client.post("/api/v1/tasks", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _create_project(client, headers, task_id: str) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/video-project",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _track(project: dict, kind: str) -> dict:
    return next(item for item in project["revision"]["timeline"]["tracks"] if item["kind"] == kind)


def _wait_for_job(client, headers, job_id: str, *, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {last}")


def _upload_and_attach_image(client, headers, project: dict) -> dict:
    uploaded = client.post(
        f"/api/v1/video-projects/{project['id']}/assets/upload",
        headers=headers,
        data={"role": "video"},
        files={"file": ("scene.png", _png_bytes(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()
    edited = client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=headers,
        json={
            "expected_revision": project["current_revision"],
            "operations": [
                {
                    "op_id": f"attach-{uuid4().hex}",
                    "type": "add",
                    "track_id": _track(project, "video")["id"],
                    "item": {
                        "id": f"scene-{uuid4().hex}",
                        "project_asset_id": asset["id"],
                        "duration_seconds": 0.6,
                    },
                }
            ],
        },
    )
    assert edited.status_code == 200, edited.text
    return edited.json()


def _upload_ai_video_reference_images(client, headers, project: dict) -> list[dict]:
    assets = []
    for index, color in enumerate(("#176b5f", "#c29b54", "#6b4b3e"), start=1):
        uploaded = client.post(
            f"/api/v1/video-projects/{project['id']}/assets/upload",
            headers=headers,
            data={"role": "video"},
            files={
                "file": (
                    f"reference-{index}.png",
                    _png_bytes(color),
                    "image/png",
                )
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        assets.append(uploaded.json())
    renamed = client.patch(
        f"/api/v1/video-projects/{project['id']}/assets/{assets[2]['id']}",
        headers=headers,
        json={"label": "已确认图片 v1"},
    )
    assert renamed.status_code == 200, renamed.text
    assets[2] = renamed.json()
    return assets


def _privacy_rejecting_ai_video_provider(*, reject_during: str):
    from content_service.llm_adapters import ProviderCallError
    from content_service.providers import AiVideoTask, ProviderStatus

    raw_error = (
        "ProviderCallError: internal-video-model HTTP 400 "
        "InputImageSensitiveContentDetected.PrivacyInformation: "
        "content[3] may contain real person; request_id=private-request-id"
    )

    class PrivacyRejectingAiVideoProvider:
        status = ProviderStatus(
            provider_id="privacy-rejecting-video-provider",
            name="privacy-rejecting-video-provider",
            capability="ai-video-generation",
            mode="test",
            external=True,
            enabled=True,
            model="internal-video-model",
        )
        capabilities = {
            "async": True,
            "text_to_video": True,
            "image_to_video": True,
            "reference_images": {"max": 9},
            "reference_videos": {"max": 3, "total_duration_seconds": 15},
            "durations": [5],
            "ratios": ["16:9"],
            "resolutions": ["720p"],
            "generate_audio": True,
            "return_last_frame": True,
        }

        def __init__(self):
            self.requests = []
            self.get_calls = []

        def submit(self, request):
            self.requests.append(request)
            if reject_during == "submit":
                raise ProviderCallError(
                    raw_error,
                    code="input_image_privacy_information",
                    retryable=False,
                    status_code=422,
                )
            return AiVideoTask(
                provider_task_id="privacy-provider-task",
                status="queued",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
                progress=0,
            )

        def get(self, provider_task_id):
            self.get_calls.append(provider_task_id)
            return AiVideoTask(
                provider_task_id=provider_task_id,
                status="failed",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
                progress=100,
                error_code="InputImageSensitiveContentDetected.PrivacyInformation",
                error_message=raw_error,
            )

        def cancel(self, provider_task_id):
            return AiVideoTask(
                provider_task_id=provider_task_id,
                status="cancelled",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
            )

    return PrivacyRejectingAiVideoProvider()


def test_ai_video_submit_privacy_rejection_is_actionable_and_leaves_no_queued_job(
    content_client,
    user_headers,
):
    from content_service.main import app
    from content_service.provider_dependencies import get_ai_video_generation_port

    provider = _privacy_rejecting_ai_video_provider(reject_during="submit")
    app.dependency_overrides[get_ai_video_generation_port] = lambda: provider
    try:
        task = _create_task(content_client, user_headers)
        project = _create_project(content_client, user_headers, task["id"])
        references = _upload_ai_video_reference_images(
            content_client,
            user_headers,
            project,
        )

        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "根据三张图片生成品牌视频",
                "reference_image_asset_ids": [item["id"] for item in references],
                "idempotency_key": f"privacy-submit-{uuid4().hex}",
            },
        )

        assert generated.status_code == 422, generated.text
        detail = generated.json()["detail"]
        assert detail["category"] == "input_content_safety"
        assert detail["code"] == "reference_image_real_person"
        assert detail["retryable"] is False
        assert detail["provider_content_index"] == 3
        assert detail["reference_image_sequence"] == 3
        assert detail["project_asset_id"] == references[2]["id"]
        assert detail["message"] == (
            "AI 视频生成未通过隐私安全检查："
            "第 3 张参考图“已确认图片 v1”可能包含真实人物。"
            "请移除或更换该图片后重试。"
        )
        assert references[2]["id"] not in detail["message"]
        for internal_detail in (
            "ProviderCallError",
            "HTTP 400",
            "internal-video-model",
            "private-request-id",
            "content[3]",
        ):
            assert internal_detail not in detail["message"]

        jobs = content_client.get(
            f"/api/v1/video-projects/{project['id']}/jobs",
            headers=user_headers,
        )
        assert jobs.status_code == 200, jobs.text
        assert jobs.json() == []
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_ai_video_poll_privacy_rejection_persists_safe_structured_error(
    content_client,
    user_headers,
):
    from content_service.main import app
    from content_service.provider_dependencies import get_ai_video_generation_port

    provider = _privacy_rejecting_ai_video_provider(reject_during="poll")
    app.dependency_overrides[get_ai_video_generation_port] = lambda: provider
    try:
        task = _create_task(content_client, user_headers)
        project = _create_project(content_client, user_headers, task["id"])
        references = _upload_ai_video_reference_images(
            content_client,
            user_headers,
            project,
        )
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "根据三张图片生成品牌视频",
                "reference_image_asset_ids": [item["id"] for item in references],
                "idempotency_key": f"privacy-poll-{uuid4().hex}",
            },
        )
        assert generated.status_code == 200, generated.text

        completed = _wait_for_job(content_client, user_headers, generated.json()["id"])
        assert completed["status"] == "failed"
        assert completed["error"] == (
            "AI 视频生成未通过隐私安全检查："
            "第 3 张参考图“已确认图片 v1”可能包含真实人物。"
            "请移除或更换该图片后重试。"
        )
        error_detail = completed["result"]["error_detail"]
        assert error_detail == {
            "category": "input_content_safety",
            "code": "reference_image_real_person",
            "retryable": False,
            "provider_content_index": 3,
            "reference_image_sequence": 3,
            "project_asset_id": references[2]["id"],
            "message": completed["error"],
            "provider_code": "InputImageSensitiveContentDetected.PrivacyInformation",
            "provider_status_code": 400,
            "provider_request_id": "private-request-id",
        }
        manifest = completed["result"]["request"]["reference_image_manifest"]
        assert [item["project_asset_id"] for item in manifest] == [
            item["id"] for item in references
        ]
        assert manifest[2]["label"] == "已确认图片 v1"
        assert provider.get_calls == ["privacy-provider-task"]
        for internal_detail in (
            "ProviderCallError",
            "HTTP 400",
            "internal-video-model",
            "private-request-id",
            "content[3]",
        ):
            assert internal_detail not in completed["error"]
    finally:
        app.dependency_overrides.pop(get_ai_video_generation_port, None)


def test_video_overlay_is_rejected_during_upload_and_timeline_edit(
    content_client,
    user_headers,
    monkeypatch,
):
    from content_service import main
    from content_service.media_ingest import MediaProbe

    task = _create_task(content_client, user_headers)
    project = _create_project(content_client, user_headers, task["id"])

    rejected_upload = content_client.post(
        f"/api/v1/video-projects/{project['id']}/assets/upload",
        headers=user_headers,
        data={"role": "overlay"},
        files={"file": ("animated-overlay.mp4", _mp4_header_bytes(), "video/mp4")},
    )
    assert rejected_upload.status_code == 422, rejected_upload.text
    assert "overlay" in rejected_upload.json()["detail"]
    rejected_resumable = content_client.post(
        "/api/v1/video-uploads",
        headers=user_headers,
        json={
            "project_id": project["id"],
            "role": "overlay",
            "filename": "animated-overlay.mp4",
            "media_type": "video/mp4",
            "total_size": len(_mp4_header_bytes()),
            "chunk_size": len(_mp4_header_bytes()),
            "checksum": "0" * 64,
        },
    )
    assert rejected_resumable.status_code == 422, rejected_resumable.text
    assert "overlay" in rejected_resumable.json()["detail"]

    monkeypatch.setattr(
        main,
        "probe_media",
        lambda *_args, **_kwargs: MediaProbe(
            kind="video",
            media_type="video/mp4",
            duration_seconds=1,
            width=96,
            height=54,
            fps=25,
            video_codec="h264",
            size_bytes=len(_mp4_header_bytes()),
        ),
    )
    monkeypatch.setattr(main, "generate_derivatives", lambda *_args, **_kwargs: ())
    uploaded = content_client.post(
        f"/api/v1/video-projects/{project['id']}/assets/upload",
        headers=user_headers,
        data={"role": "video"},
        files={"file": ("scene.mp4", _mp4_header_bytes(), "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["metadata"]["duration_seconds"] == 1

    rejected_edit = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "operations": [
                {
                    "op_id": "reject-video-overlay",
                    "type": "add",
                    "track_id": _track(project, "overlay")["id"],
                    "item": {
                        "id": "animated-overlay",
                        "project_asset_id": uploaded.json()["id"],
                        "duration_seconds": 1,
                    },
                }
            ],
        },
    )
    assert rejected_edit.status_code == 422, rejected_edit.text
    assert "overlay" in rejected_edit.json()["detail"]


def test_enterprise_long_video_duration_is_exposed_backfilled_and_attached(
    content_client,
    user_headers,
    monkeypatch,
):
    from sqlalchemy import select

    from content_service import main
    from content_service.media_ingest import MediaProbe
    from content_service.models import AssetRef, LibraryItem, ResourceEntry, ResourceVersion
    from content_service.services import dump, load

    category = f"long-video-{uuid4().hex}"
    probe_duration = {"value": 309.17}

    def fake_probe(*_args, **_kwargs):
        return MediaProbe(
            kind="video",
            media_type="video/mp4",
            duration_seconds=probe_duration["value"],
            width=856,
            height=480,
            fps=25,
            video_codec="h264",
            audio_codec="aac",
            size_bytes=len(_mp4_header_bytes()),
        )

    monkeypatch.setattr(main, "probe_media", fake_probe)
    uploaded = content_client.post(
        "/api/v1/library/items/upload",
        headers=user_headers,
        params={
            "resource_kind": "video",
            "category": category,
            "name": "企业长视频",
        },
        files={"file": ("enterprise.mp4", _mp4_header_bytes(), "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text
    library_item = uploaded.json()
    assert library_item["metadata"]["duration_seconds"] == 309.17
    assert library_item["metadata"]["probe"]["duration_seconds"] == 309.17

    # Simulate a media row imported before duration probing was introduced.
    probe_duration["value"] = 287.25
    with main.SessionLocal() as session:
        item = session.get(LibraryItem, library_item["id"])
        assert item is not None
        entry = session.get(ResourceEntry, item.resource_entry_id)
        assert entry is not None
        version = session.get(ResourceVersion, entry.current_version_id)
        assert version is not None
        asset = session.scalar(select(AssetRef).where(AssetRef.storage_key == item.storage_key))
        assert asset is not None
        legacy_metadata = {
            "legacy_library_item_id": item.id,
            "filename": "enterprise.mp4",
        }
        entry.metadata_json = dump(legacy_metadata)
        version.metadata_json = dump(legacy_metadata)
        asset.metadata_json = "{}"
        session.commit()

    listed = content_client.get(
        "/api/v1/library/items",
        headers=user_headers,
        params={"category": category},
    )
    assert listed.status_code == 200, listed.text
    refreshed = listed.json()[0]
    assert refreshed["metadata"]["duration_seconds"] == 287.25
    assert refreshed["metadata"]["probe_status"] == "succeeded"
    detail = content_client.get(
        f"/api/v1/library/items/{library_item['id']}",
        headers=user_headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["metadata"]["duration_seconds"] == 287.25
    with main.SessionLocal() as session:
        version = session.get(ResourceVersion, library_item["resource_version_id"])
        assert version is not None
        assert load(version.metadata_json)["duration_seconds"] == 287.25

    task = _create_task(content_client, user_headers)
    project = _create_project(content_client, user_headers, task["id"])
    attached = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "operations": [
                {
                    "op_id": "attach-enterprise-long-video",
                    "type": "add",
                    "track_id": _track(project, "video")["id"],
                    "item": {
                        "id": "enterprise-long-video",
                        "library_item_id": library_item["id"],
                        "duration_seconds": 287.25,
                    },
                }
            ],
        },
    )
    assert attached.status_code == 200, attached.text
    attached_asset = next(
        item for item in attached.json()["assets"] if item["library_item_id"] == library_item["id"]
    )
    assert attached_asset["metadata"]["duration_seconds"] == 287.25
    assert _track(attached.json(), "video")["items"][0]["duration_seconds"] == 287.25


def test_periodic_recovery_waits_for_live_lease_then_dispatches_expired_job(
    content_client,
    user_headers,
    monkeypatch,
):
    from content_service import main
    from content_service.models import Job
    from content_service.video_project_jobs import RENDER_JOB_KIND

    task = _create_task(content_client, user_headers)
    job_id = str(uuid4())
    with main.SessionLocal() as session:
        session.add(
            Job(
                id=job_id,
                task_id=task["id"],
                owner_id="user-a",
                kind=RENDER_JOB_KIND,
                status="running",
                progress=42,
                result_json="{}",
                attempt=1,
                max_attempts=3,
                lease_owner="departed-worker",
                lease_token="departed-token",
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        session.commit()

    class RecordingDispatcher:
        def __init__(self):
            self.callbacks = []

        def submit(self, callback):
            self.callbacks.append(callback)

    recording_dispatcher = RecordingDispatcher()
    monkeypatch.setattr(main, "dispatcher", recording_dispatcher)
    assert main.recover_expired_video_project_jobs() == 0
    assert recording_dispatcher.callbacks == []

    with main.SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    assert main.recover_expired_video_project_jobs() == 1
    assert len(recording_dispatcher.callbacks) == 1
    with main.SessionLocal() as session:
        recovered = session.get(Job, job_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.lease_owner is None
        assert recovered.lease_token is None


def test_project_render_and_selective_export_routes_are_durable_and_downloadable(
    content_client,
    user_headers,
    other_headers,
):
    task = _create_task(content_client, user_headers)
    project = _upload_and_attach_image(
        content_client,
        user_headers,
        _create_project(content_client, user_headers, task["id"]),
    )
    render_payload = {
        "expected_revision": project["current_revision"],
        "profile": "preview-480p",
        "fps": 30,
        "subtitle_mode": "none",
        "include_audio": False,
        "idempotency_key": f"render-{uuid4().hex}",
    }
    created = content_client.post(
        f"/api/v1/video-projects/{project['id']}/renders",
        headers=user_headers,
        json=render_payload,
    )
    assert created.status_code == 200, created.text
    render = _wait_for_job(content_client, user_headers, created.json()["id"])
    assert render["status"] == "succeeded", render
    assert render["result"]["download_url"].endswith("/download")
    assert "storage_key" not in render["result"]
    preview = content_client.get(render["result"]["download_url"], headers=user_headers)
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("video/mp4")
    assert b"ftyp" in preview.content[:32]
    repeated = content_client.post(
        f"/api/v1/video-projects/{project['id']}/renders",
        headers=user_headers,
        json=render_payload,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["id"] == render["id"]
    conflicting = content_client.post(
        f"/api/v1/video-projects/{project['id']}/renders",
        headers=user_headers,
        json={**render_payload, "include_audio": True},
    )
    assert conflicting.status_code == 409, conflicting.text
    assert "幂等键" in conflicting.json()["detail"]
    assert (
        content_client.get(f"/api/v1/jobs/{render['id']}", headers=other_headers).status_code == 403
    )

    exported = content_client.post(
        f"/api/v1/video-projects/{project['id']}/exports",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "deliverables": ["mixed_video", "silent_video", "clips", "project_zip"],
            "video_profile": "480p",
            "fps": 30,
            "subtitle_mode": "none",
            "include_original_assets": True,
            "idempotency_key": f"export-{uuid4().hex}",
        },
    )
    assert exported.status_code == 200, exported.text
    export_job = _wait_for_job(content_client, user_headers, exported.json()["id"])
    assert export_job["status"] == "succeeded", export_job
    bundle = content_client.get(
        export_job["result"]["download_url"],
        headers=user_headers,
    )
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "checksums.sha256" in names
        assert any(name.endswith(".mp4") for name in names)
    listed = content_client.get(
        f"/api/v1/video-projects/{project['id']}/jobs",
        headers=user_headers,
    )
    assert listed.status_code == 200, listed.text
    assert {item["id"] for item in listed.json()} >= {render["id"], export_job["id"]}


def test_final_export_requires_brief_but_preview_does_not(content_client, user_headers):
    task = _create_task(content_client, user_headers, complete_brief=False)
    project = _upload_and_attach_image(
        content_client,
        user_headers,
        _create_project(content_client, user_headers, task["id"]),
    )
    preview = content_client.post(
        f"/api/v1/video-projects/{project['id']}/renders",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "profile": "preview-480p",
            "subtitle_mode": "none",
            "include_audio": False,
        },
    )
    assert preview.status_code == 200, preview.text
    blocked = content_client.post(
        f"/api/v1/video-projects/{project['id']}/exports",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "deliverables": ["mixed_video"],
            "video_profile": "480p",
        },
    )
    assert blocked.status_code == 422
    assert "目标人群" in blocked.json()["detail"]


def test_editor_ai_assets_and_logo_confirmation_flow(content_client, user_headers):
    task = _create_task(content_client, user_headers)
    project = _create_project(content_client, user_headers, task["id"])
    voice = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-voice",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "text": "雅酿酒业，敬每一份努力。",
            "audio_format": "wav",
        },
    )
    assert voice.status_code == 200, voice.text
    assert voice.json()["role"] == "voice"
    assert voice.json()["metadata"]["source"] == "ai-generated"

    images = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-images",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "prompt": "绿色圆形品牌 Logo 候选，透明背景",
            "role": "logo",
            "count": 1,
        },
    )
    assert images.status_code == 200, images.text
    logo = images.json()[0]
    assert logo["metadata"]["pending_brand_confirmation"] is True
    blocked = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "operations": [
                {
                    "op_id": "use-unconfirmed-logo",
                    "type": "add",
                    "track_id": _track(project, "overlay")["id"],
                    "item": {
                        "id": "logo-candidate",
                        "project_asset_id": logo["id"],
                        "duration_seconds": 2,
                    },
                }
            ],
        },
    )
    assert blocked.status_code == 409
    confirmed = content_client.post(
        f"/api/v1/video-projects/{project['id']}/assets/{logo['id']}/confirm-brand",
        headers=user_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["metadata"]["pending_brand_confirmation"] is False
    adopted = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "operations": [
                {
                    "op_id": "use-confirmed-logo",
                    "type": "add",
                    "track_id": _track(project, "overlay")["id"],
                    "item": {
                        "id": "logo-confirmed",
                        "project_asset_id": logo["id"],
                        "duration_seconds": 2,
                    },
                }
            ],
        },
    )
    assert adopted.status_code == 200, adopted.text

    video = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-video",
        headers=user_headers,
        json={
            "expected_revision": adopted.json()["current_revision"],
            "prompt": "镜头缓慢推进，保持品牌画面稳定",
            "duration_seconds": 5,
            "ratio": "16:9",
            "resolution": "480p",
            "idempotency_key": f"ai-video-{uuid4().hex}",
        },
    )
    assert video.status_code == 200, video.text
    completed = _wait_for_job(content_client, user_headers, video.json()["id"])
    assert completed["status"] == "succeeded", completed.get("error") or completed
    assert completed["result"]["project_asset_id"]
    assets = content_client.get(
        f"/api/v1/video-projects/{project['id']}/assets",
        headers=user_headers,
    ).json()
    generated_video = next(
        item for item in assets if item["id"] == completed["result"]["project_asset_id"]
    )
    assert generated_video["role"] == "video"
    assert generated_video["metadata"]["source"] == "ai-generated"


def test_editor_generates_subtitle_asset_from_voice_and_returns_ready_edit(
    content_client,
    user_headers,
):
    task = _create_task(content_client, user_headers)
    project = _create_project(content_client, user_headers, task["id"])
    voice_response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-voice",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "text": "雅酿酒业，敬每一份努力。",
            "speed": 1.25,
            "audio_format": "wav",
            "with_timestamps": True,
        },
    )
    assert voice_response.status_code == 200, voice_response.text
    voice = voice_response.json()
    assert voice["metadata"]["duration_seconds"] > 0
    assert voice["metadata"]["speed"] == 1.25
    assert voice["metadata"]["probe_status"] == "succeeded"

    subtitle_response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-subtitles",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "source_project_asset_id": voice["id"],
            "label": "口播字幕",
        },
    )
    assert subtitle_response.status_code == 200, subtitle_response.text
    generated = subtitle_response.json()
    assert generated["asset"]["role"] == "subtitle"
    assert generated["asset"]["label"] == "口播字幕"
    assert generated["source_project_asset_id"] == voice["id"]
    assert generated["segments"]
    assert generated["asset"]["metadata"]["segments"] == generated["segments"]
    assert generated["asset"]["metadata"]["text"] == generated["text"]
    assert generated["asset"]["metadata"]["duration_seconds"] == generated["duration_seconds"]
    downloaded = content_client.get(
        generated["asset"]["original_url"],
        headers=user_headers,
    )
    assert downloaded.status_code == 200, downloaded.text
    assert " --> " in downloaded.text
    suggested_edit = generated["suggested_edit"]
    assert suggested_edit["expected_revision"] == project["current_revision"]
    operation = suggested_edit["operations"][0]
    assert operation["track_kind"] == "subtitle"
    assert operation["item"]["project_asset_id"] == generated["asset"]["id"]
    assert operation["item"]["start_seconds"] == 0
    assert operation["item"]["duration_seconds"] == generated["duration_seconds"]
    assert operation["item"]["speed"] == 1

    edited_response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json=suggested_edit,
    )
    assert edited_response.status_code == 200, edited_response.text
    subtitle_track = _track(edited_response.json(), "subtitle")
    assert subtitle_track["items"][0]["project_asset_id"] == generated["asset"]["id"]
    assert subtitle_track["items"][0]["duration_seconds"] == generated["duration_seconds"]
    assert subtitle_track["items"][0]["speed"] == 1

    playback_speed = 1.5
    voice_duration = round(
        voice["metadata"]["duration_seconds"] / playback_speed,
        6,
    )
    voice_edit = content_client.post(
        f"/api/v1/video-projects/{project['id']}/edits",
        headers=user_headers,
        json={
            "expected_revision": edited_response.json()["current_revision"],
            "operations": [
                {
                    "op_id": "add-generated-voice-with-speed",
                    "type": "add",
                    "track_kind": "voice",
                    "item": {
                        "id": "generated-voice-with-speed",
                        "project_asset_id": voice["id"],
                        "duration_seconds": voice_duration,
                        "speed": playback_speed,
                    },
                }
            ],
        },
    )
    assert voice_edit.status_code == 200, voice_edit.text
    saved_voice = _track(voice_edit.json(), "voice")["items"][0]
    assert saved_voice["duration_seconds"] == voice_duration
    assert saved_voice["speed"] == playback_speed
