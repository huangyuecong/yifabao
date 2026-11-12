from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import pytest

from content_service import main as content_main
from content_service.config import load_settings
from content_service.media_ingest import MediaProbe
from content_service.provider_dependencies import (
    get_ai_video_generation_port,
    get_provider_media_publisher,
)
from content_service.provider_media_publishers import (
    ProviderMediaPublicationError,
    TosProviderMediaPublisher,
)
from content_service.providers import (
    AiVideoGenerationRequest,
    AiVideoTask,
    ProviderMediaPublication,
    ProviderStatus,
)

MP4 = b"\x00\x00\x00\x18ftypisom" + b"private-reference-video"
TOS_ENV_NAMES = (
    "CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT",
    "CONTENT_PROVIDER_MEDIA_TOS_REGION",
    "CONTENT_PROVIDER_MEDIA_TOS_BUCKET",
    "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY",
    "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY_FILE",
    "CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY",
    "CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY_FILE",
    "CONTENT_PROVIDER_MEDIA_TOS_SECURITY_TOKEN",
    "CONTENT_PROVIDER_MEDIA_TOS_SECURITY_TOKEN_FILE",
    "CONTENT_PROVIDER_MEDIA_TOS_KEY_PREFIX",
    "CONTENT_PROVIDER_MEDIA_TOS_URL_TTL_SECONDS",
    "CONTENT_PROVIDER_MEDIA_TOS_OBJECT_TTL_DAYS",
    "CONTENT_PROVIDER_MEDIA_TOS_CONNECT_TIMEOUT_SECONDS",
    "CONTENT_PROVIDER_MEDIA_TOS_SOCKET_TIMEOUT_SECONDS",
)


class FakeTosClient:
    def __init__(self):
        self.puts = []
        self.deletes = []
        self.presigns = []
        self.listed = []
        self.signed_url = "https://private-bucket.tos.example/object?X-Tos-Signature=secret"

    def put_object(self, bucket, key, **kwargs):
        source = kwargs["content"]
        assert not isinstance(source, (bytes, bytearray))
        body = source.read()
        self.puts.append({"bucket": bucket, "key": key, "body": body, **kwargs})
        return SimpleNamespace(status_code=200)

    def pre_signed_url(self, method, bucket, key, *, expires):
        self.presigns.append({"method": method, "bucket": bucket, "key": key, "expires": expires})
        return SimpleNamespace(signed_url=self.signed_url)

    def delete_object(self, bucket, key):
        self.deletes.append((bucket, key))
        return SimpleNamespace(status_code=204)

    def list_objects_type2(self, bucket, **kwargs):
        assert bucket == "private-bucket"
        assert kwargs == {
            "prefix": "provider-media/reference-videos/",
            "max_keys": 1000,
            "list_only_once": True,
        }
        return SimpleNamespace(contents=[SimpleNamespace(key=key) for key in self.listed])


def make_publisher(tmp_path: Path, client: FakeTosClient, *, now: datetime):
    return TosProviderMediaPublisher(
        endpoint="https://tos.example",
        region="cn-test",
        bucket="private-bucket",
        access_key="test-access-key",
        secret_key="test-secret-key",
        security_token=None,
        key_prefix="provider-media/reference-videos",
        url_ttl_seconds=600,
        object_ttl_days=1,
        allowed_root=tmp_path / "objects",
        client=client,
        clock=lambda: now,
    )


def test_tos_publisher_streams_private_object_and_returns_short_lived_https_url(tmp_path):
    now = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
    client = FakeTosClient()
    publisher = make_publisher(tmp_path, client, now=now)
    source = tmp_path / "objects" / "opaque-storage-object"
    source.parent.mkdir(parents=True)
    source.write_bytes(MP4)

    publication = publisher.publish(
        source,
        media_type="video/mp4",
        publication_id="job-id:asset-id",
    )

    assert publication.url == client.signed_url
    assert publication.transport == "tos-private-presigned-https"
    assert int((publication.expires_at - now).total_seconds()) == 600
    assert publication.cleanup_token == client.puts[0]["key"]
    uploaded = client.puts[0]
    assert uploaded["body"] == MP4
    assert uploaded["content_length"] == len(MP4)
    assert uploaded["content_type"] == "video/mp4"
    assert uploaded["acl"] == "private"
    assert uploaded["forbid_overwrite"] is True
    assert uploaded["object_expires"] == 1
    assert uploaded["key"].startswith(
        f"provider-media/reference-videos/{int(publication.expires_at.timestamp())}/"
    )
    assert str(source) not in uploaded["key"]
    assert client.presigns == [
        {
            "method": "GET",
            "bucket": "private-bucket",
            "key": uploaded["key"],
            "expires": 600,
        }
    ]
    publisher.release(publication.cleanup_token)
    assert client.deletes == [("private-bucket", publication.cleanup_token)]


def test_tos_publisher_fails_closed_and_deletes_upload_when_presign_is_invalid(tmp_path):
    client = FakeTosClient()
    client.signed_url = "http://public.example/object"
    publisher = make_publisher(
        tmp_path,
        client,
        now=datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
    )
    source = tmp_path / "objects" / "source"
    source.parent.mkdir(parents=True)
    source.write_bytes(MP4)

    with pytest.raises(ProviderMediaPublicationError, match="预签名 HTTPS"):
        publisher.publish(source, media_type="video/mp4", publication_id="job")

    assert client.deletes == [("private-bucket", client.puts[0]["key"])]


def test_tos_publisher_rejects_outside_root_and_non_video_without_upload(tmp_path):
    client = FakeTosClient()
    publisher = make_publisher(
        tmp_path,
        client,
        now=datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
    )
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(MP4)
    with pytest.raises(ProviderMediaPublicationError, match="不在内容素材目录"):
        publisher.publish(outside, media_type="video/mp4", publication_id="job")

    inside = tmp_path / "objects" / "not-video"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"not a video")
    with pytest.raises(ProviderMediaPublicationError, match="格式校验失败"):
        publisher.publish(inside, media_type="video/mp4", publication_id="job")
    assert client.puts == []


def test_tos_publisher_releases_only_fixed_prefix_and_sweeps_expired_keys(tmp_path):
    now = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
    now_epoch = int(now.timestamp())
    client = FakeTosClient()
    publisher = make_publisher(tmp_path, client, now=now)
    expired = f"provider-media/reference-videos/{now_epoch - 1}/{'a' * 24}/{'b' * 32}.mp4"
    future = f"provider-media/reference-videos/{now_epoch + 60}/{'c' * 24}/{'d' * 32}.mp4"
    client.listed = [expired, future, "other-prefix/1/job/c.mp4", "invalid"]

    assert publisher.purge_expired() == 1
    assert client.deletes == [("private-bucket", expired)]
    with pytest.raises(ProviderMediaPublicationError, match="清理标识无效"):
        publisher.release("unrelated/private-object.mp4")
    assert client.deletes == [("private-bucket", expired)]


def test_tos_settings_are_all_or_nothing_and_keep_credentials_out_of_repr(
    monkeypatch,
    tmp_path,
):
    for name in TOS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    disabled = load_settings()
    assert disabled.provider_media_tos_endpoint is None
    assert disabled.provider_media_tos_url_ttl_seconds == 900
    assert disabled.provider_media_tos_object_ttl_days == 1

    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT", "https://tos.example")
    with pytest.raises(ValueError, match="配置不完整"):
        load_settings()

    access_key_file = tmp_path / "tos-ak"
    secret_key_file = tmp_path / "tos-sk"
    access_key_file.write_text("private-access-key", encoding="utf-8")
    secret_key_file.write_text("private-secret-key", encoding="utf-8")
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_REGION", "cn-test")
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_BUCKET", "private-bucket")
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY_FILE", str(access_key_file))
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY_FILE", str(secret_key_file))
    enabled = load_settings()
    assert enabled.provider_media_tos_endpoint == "https://tos.example"
    assert enabled.provider_media_tos_bucket == "private-bucket"
    assert "private-access-key" not in repr(enabled)
    assert "private-secret-key" not in repr(enabled)

    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY", "duplicate-source")
    with pytest.raises(ValueError, match="只能配置一种"):
        load_settings()
    monkeypatch.delenv("CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY")
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT", "http://tos.example")
    with pytest.raises(ValueError, match="HTTPS"):
        load_settings()
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT", "https://tos.example")
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_TOS_URL_TTL_SECONDS", "30")
    with pytest.raises(ValueError, match="60 到 3600"):
        load_settings()


def test_capabilities_prefer_tos_and_keep_legacy_transport_boolean(
    content_client,
    user_headers,
):
    class ExternalVideoProvider:
        status = ProviderStatus(
            provider_id="external-video",
            name="external-video",
            capability="ai-video-generation",
            mode="api",
            external=True,
            enabled=True,
            model="seedance",
        )
        capabilities: ClassVar[dict] = {
            "reference_images": {"max": 9},
            "reference_videos": {"max": 3, "total_duration_seconds": 15},
        }

    class EnabledPublisher:
        enabled = True
        transport = "tos-private-presigned-https"
        url_ttl_seconds = 480

        def purge_expired(self):
            return 0

    original_settings = content_main.settings
    content_main.settings = replace(
        original_settings,
        provider_media_base_url="https://content.example/api/v1/provider-media",
        provider_media_signing_secret="x" * 32,
    )
    content_main.app.dependency_overrides[get_ai_video_generation_port] = ExternalVideoProvider
    content_main.app.dependency_overrides[get_provider_media_publisher] = EnabledPublisher
    try:
        response = content_client.get("/api/v1/capabilities", headers=user_headers)
        assert response.status_code == 200, response.text
        video = next(
            item
            for item in response.json()["capabilities"]
            if item["capability"] == "ai-video-generation"
        )
        references = video["parameters"]["reference_videos"]
        assert references["project_asset_transport_available"] is True
        assert references["project_asset_transport"] == "tos-private-presigned-https"
        assert references["project_asset_transports"] == [
            "tos-private-presigned-https",
            "provider-signed-https-gateway",
        ]
        assert references["project_asset_url_ttl_seconds"] == 480
        assert references["private_temporary_object"] is True
    finally:
        content_main.settings = original_settings
        content_main.app.dependency_overrides.pop(get_ai_video_generation_port, None)
        content_main.app.dependency_overrides.pop(get_provider_media_publisher, None)


def test_capabilities_keep_provider_native_reference_when_url_transport_is_unavailable(
    content_client,
    user_headers,
):
    class ExternalVideoProvider:
        status = ProviderStatus(
            provider_id="external-video",
            name="external-video",
            capability="ai-video-generation",
            mode="api",
            external=True,
            enabled=True,
            model="seedance",
        )
        capabilities: ClassVar[dict] = {
            "reference_videos": {
                "max": 3,
                "provider_native_reference_supported": True,
                "provider_native_reference_transport": "provider-managed-output-https",
            },
        }

    class DisabledPublisher:
        enabled = False

        def purge_expired(self):
            return 0

    original_settings = content_main.settings
    content_main.settings = replace(
        original_settings,
        provider_media_base_url=None,
        provider_media_signing_secret=None,
    )
    content_main.app.dependency_overrides[get_ai_video_generation_port] = ExternalVideoProvider
    content_main.app.dependency_overrides[get_provider_media_publisher] = DisabledPublisher
    try:
        response = content_client.get("/api/v1/capabilities", headers=user_headers)
        assert response.status_code == 200, response.text
        video = next(
            item
            for item in response.json()["capabilities"]
            if item["capability"] == "ai-video-generation"
        )
        references = video["parameters"]["reference_videos"]
        assert references["project_asset_transport_available"] is False
        assert references["project_asset_transport"] == "unavailable"
        assert references["provider_native_reference_supported"] is True
        assert references["provider_native_reference_transport"] == (
            "provider-managed-output-https"
        )
    finally:
        content_main.settings = original_settings
        content_main.app.dependency_overrides.pop(get_ai_video_generation_port, None)
        content_main.app.dependency_overrides.pop(get_provider_media_publisher, None)


def test_generate_video_route_publishes_selected_project_video_without_leaking_url(
    content_client,
    user_headers,
    monkeypatch,
):
    class RouteVideoProvider:
        status = ProviderStatus(
            provider_id="route-seedance",
            name="route-seedance",
            capability="ai-video-generation",
            mode="test",
            external=True,
            enabled=True,
            model="seedance-test",
        )
        capabilities: ClassVar[dict] = {
            "async": True,
            "text_to_video": True,
            "reference_videos": {"max": 3, "total_duration_seconds": 15},
            "durations": [5],
            "ratios": ["16:9"],
            "resolutions": ["720p"],
        }

        def __init__(self):
            self.requests: list[AiVideoGenerationRequest] = []
            self.cancelled = []

        def submit(self, request):
            self.requests.append(request)
            return AiVideoTask(
                provider_task_id=uuid4().hex,
                status="queued",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
            )

        def get(self, provider_task_id):
            return AiVideoTask(
                provider_task_id=provider_task_id,
                status="failed",
                provider_id=self.status.provider_id,
                model=self.status.model or "",
                error_message="route test terminal state",
            )

        def cancel(self, provider_task_id):
            self.cancelled.append(provider_task_id)
            return self.get(provider_task_id)

    class RoutePublisher:
        enabled = True
        transport = "tos-private-presigned-https"
        url_ttl_seconds = 600

        def __init__(self):
            self.paths = []
            self.released = []

        def publish(self, source_path, *, media_type, publication_id):
            self.paths.append((source_path, media_type, publication_id))
            return ProviderMediaPublication(
                url="https://private.example/reference.mp4?X-Tos-Signature=route-secret",
                transport=self.transport,
                expires_at=datetime.now(UTC),
                cleanup_token="provider-media/reference-videos/1/job/reference.mp4",
            )

        def release(self, cleanup_token):
            self.released.append(cleanup_token)

        def purge_expired(self):
            return 0

    monkeypatch.setattr(
        content_main,
        "probe_media",
        lambda *_args, **_kwargs: MediaProbe(
            kind="video",
            media_type="video/mp4",
            duration_seconds=5,
            width=1280,
            height=720,
            fps=25,
            video_codec="h264",
            size_bytes=len(MP4),
        ),
    )
    monkeypatch.setattr(content_main, "generate_derivatives", lambda *_args, **_kwargs: ())
    task_response = content_client.post(
        "/api/v1/tasks",
        headers=user_headers,
        json={
            "mode": "video",
            "name": f"TOS route {uuid4().hex[:8]}",
            "topic": "品牌视频",
            "content_type": "短视频",
            "style": "品牌宣传型",
            "audience": "成年消费者",
        },
    )
    assert task_response.status_code == 200, task_response.text
    project_response = content_client.post(
        f"/api/v1/tasks/{task_response.json()['id']}/video-project",
        headers=user_headers,
    )
    assert project_response.status_code == 200, project_response.text
    project = project_response.json()
    uploaded = content_client.post(
        f"/api/v1/video-projects/{project['id']}/assets/upload",
        headers=user_headers,
        data={"role": "video"},
        files={"file": ("reference.mp4", MP4, "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    provider = RouteVideoProvider()
    publisher = RoutePublisher()
    content_main.app.dependency_overrides[get_ai_video_generation_port] = lambda: provider
    content_main.app.dependency_overrides[get_provider_media_publisher] = lambda: publisher
    try:
        generated = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "参考工程视频生成",
                "reference_video_asset_ids": [uploaded.json()["id"]],
                "duration_seconds": 5,
                "ratio": "16:9",
                "resolution": "720p",
            },
        )
        assert generated.status_code == 200, generated.text
        assert len(publisher.paths) == 1
        assert provider.requests[0].reference_video_urls == (
            "https://private.example/reference.mp4?X-Tos-Signature=route-secret",
        )
        body = generated.text
        assert "X-Tos-Signature" not in body
        assert "_provider_media_publications" not in body
        assert "cleanup_token" not in body
        deadline = time.monotonic() + 3
        while not publisher.released and time.monotonic() < deadline:
            time.sleep(0.01)
        assert publisher.released == ["provider-media/reference-videos/1/job/reference.mp4"]

        monkeypatch.setattr(content_main.dispatcher, "submit", lambda _operation: None)
        queued_for_cancel = content_client.post(
            f"/api/v1/video-projects/{project['id']}/generate-video",
            headers=user_headers,
            json={
                "expected_revision": project["current_revision"],
                "prompt": "随后取消的参考视频任务",
                "reference_video_asset_ids": [uploaded.json()["id"]],
                "duration_seconds": 5,
                "ratio": "16:9",
                "resolution": "720p",
            },
        )
        assert queued_for_cancel.status_code == 200, queued_for_cancel.text
        cancelled = content_client.post(
            f"/api/v1/jobs/{queued_for_cancel.json()['id']}/cancel",
            headers=user_headers,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        assert len(provider.cancelled) == 1
        assert publisher.released == [
            "provider-media/reference-videos/1/job/reference.mp4",
            "provider-media/reference-videos/1/job/reference.mp4",
        ]
        retried = content_client.post(
            f"/api/v1/jobs/{queued_for_cancel.json()['id']}/retry",
            headers=user_headers,
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["id"] != queued_for_cancel.json()["id"]
        assert len(publisher.paths) == 3
        assert len({item[2] for item in publisher.paths}) == 3
        assert "X-Tos-Signature" not in retried.text
        assert "_provider_media_publications" not in retried.text
        retry_cancelled = content_client.post(
            f"/api/v1/jobs/{retried.json()['id']}/cancel",
            headers=user_headers,
        )
        assert retry_cancelled.status_code == 200, retry_cancelled.text
        assert retry_cancelled.json()["status"] == "cancelled"
    finally:
        content_main.app.dependency_overrides.pop(get_ai_video_generation_port, None)
        content_main.app.dependency_overrides.pop(get_provider_media_publisher, None)
