from __future__ import annotations

from dataclasses import replace
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from content_service import main as content_main
from content_service.config import load_settings
from content_service.media_adapters import LocalAssetStore
from content_service.provider_dependencies import (
    get_ai_video_generation_port,
    get_asset_store_port,
    get_provider_media_url_signer,
)
from content_service.provider_media_gateway import ProviderMediaUrlSigner
from content_service.providers import ProviderStatus

_TEST_SECRET = "provider-media-test-secret-that-is-long-enough"


def test_signed_provider_media_url_binds_key_and_expiry(tmp_path):
    now = [1_000.0]
    signer = ProviderMediaUrlSigner(
        base_url="https://content.example.cn/api/v1/provider-media",
        signing_secret=_TEST_SECRET,
        ttl_seconds=300,
        clock=lambda: now[0],
    )
    store = LocalAssetStore(
        tmp_path / "objects",
        provider_media_url_signer=signer,
    )
    key = "video-projects/项目 A/originals/参考 1.mp4"
    store.put_bytes(key, b"0123456789", "video/mp4")

    provider_url = store.provider_url(key)
    assert provider_url is not None
    parsed = urlsplit(provider_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.path.endswith(
        "/video-projects/%E9%A1%B9%E7%9B%AE%20A/originals/%E5%8F%82%E8%80%83%201.mp4"
    )
    assert query["expires"] == ["1300"]
    signature = query["signature"][0]
    assert signer.verify(key, expires=1_300, signature=signature)
    assert not signer.verify(f"{key}.changed", expires=1_300, signature=signature)

    now[0] = 1_301
    assert not signer.verify(key, expires=1_300, signature=signature)


def test_provider_media_signer_fails_closed_for_partial_or_unsafe_configuration():
    assert ProviderMediaUrlSigner().enabled is False
    with pytest.raises(ValueError, match="同时配置"):
        ProviderMediaUrlSigner(
            base_url="https://content.example.cn/api/v1/provider-media",
        )
    with pytest.raises(ValueError, match="同时配置"):
        ProviderMediaUrlSigner(signing_secret=_TEST_SECRET)
    with pytest.raises(ValueError, match="HTTPS"):
        ProviderMediaUrlSigner(
            base_url="http://content.example.cn/api/v1/provider-media",
            signing_secret=_TEST_SECRET,
        )
    with pytest.raises(ValueError, match="查询串"):
        ProviderMediaUrlSigner(
            base_url="https://content.example.cn/api/v1/provider-media?token=unsafe",
            signing_secret=_TEST_SECRET,
        )
    with pytest.raises(ValueError, match="32"):
        ProviderMediaUrlSigner(
            base_url="https://content.example.cn/api/v1/provider-media",
            signing_secret="too-short",
        )


def test_provider_media_gateway_serves_only_valid_signed_video_urls(
    content_client,
    tmp_path,
):
    now = [2_000.0]
    signer = ProviderMediaUrlSigner(
        base_url="https://content.example.cn/api/v1/provider-media",
        signing_secret=_TEST_SECRET,
        ttl_seconds=300,
        clock=lambda: now[0],
    )
    store = LocalAssetStore(
        tmp_path / "gateway-objects",
        provider_media_url_signer=signer,
    )
    key = "video-projects/project-a/originals/参考 视频.mp4"
    store.put_bytes(key, b"0123456789", "video/mp4")
    text_key = "video-projects/project-a/originals/notes.txt"
    store.put_bytes(text_key, b"private notes", "text/plain")
    content_main.app.dependency_overrides[get_asset_store_port] = lambda: store
    content_main.app.dependency_overrides[get_provider_media_url_signer] = lambda: signer
    try:
        provider_url = store.provider_url(key)
        assert provider_url is not None
        parsed = urlsplit(provider_url)
        target = f"{parsed.path}?{parsed.query}"

        response = content_client.get(target)
        assert response.status_code == 200, response.text
        assert response.content == b"0123456789"
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert response.headers["x-content-type-options"] == "nosniff"

        head = content_client.head(target)
        assert head.status_code == 200, head.text
        assert head.content == b""
        assert head.headers["content-length"] == "10"

        ranged = content_client.get(target, headers={"Range": "bytes=2-5"})
        assert ranged.status_code == 206, ranged.text
        assert ranged.content == b"2345"

        tampered = content_client.get(
            target.replace(parse_qs(parsed.query)["signature"][0], "0" * 64)
        )
        assert tampered.status_code == 403

        text_url = store.provider_url(text_key)
        assert text_url is not None
        parsed_text = urlsplit(text_url)
        blocked_text = content_client.get(f"{parsed_text.path}?{parsed_text.query}")
        assert blocked_text.status_code == 404

        now[0] = 2_301
        expired = content_client.get(target)
        assert expired.status_code == 403
    finally:
        content_main.app.dependency_overrides.pop(get_asset_store_port, None)
        content_main.app.dependency_overrides.pop(get_provider_media_url_signer, None)


def test_provider_media_settings_and_external_capability_are_fail_closed(
    content_client,
    user_headers,
    monkeypatch,
    tmp_path,
):
    for name in (
        "CONTENT_PROVIDER_MEDIA_BASE_URL",
        "CONTENT_PROVIDER_MEDIA_SIGNING_SECRET",
        "CONTENT_PROVIDER_MEDIA_SIGNING_SECRET_FILE",
        "CONTENT_PROVIDER_MEDIA_URL_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    disabled = load_settings()
    assert disabled.provider_media_base_url is None
    assert disabled.provider_media_signing_secret is None
    assert disabled.provider_media_url_ttl_seconds == 900

    monkeypatch.setenv(
        "CONTENT_PROVIDER_MEDIA_BASE_URL",
        "https://content.example.cn/api/v1/provider-media",
    )
    with pytest.raises(ValueError, match="同时配置"):
        load_settings()
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_SIGNING_SECRET", _TEST_SECRET)
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_URL_TTL_SECONDS", "600")
    enabled = load_settings()
    assert enabled.provider_media_url_ttl_seconds == 600
    assert _TEST_SECRET not in repr(enabled)

    secret_file = tmp_path / "provider-media-secret"
    secret_file.write_text(_TEST_SECRET, encoding="utf-8")
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_SIGNING_SECRET_FILE", str(secret_file))
    with pytest.raises(ValueError, match="只能配置"):
        load_settings()
    monkeypatch.delenv("CONTENT_PROVIDER_MEDIA_SIGNING_SECRET_FILE")

    class ExternalVideoProvider:
        status = ProviderStatus(
            provider_id="external-video",
            name="external-video",
            capability="ai-video-generation",
            mode="api",
            external=True,
            enabled=True,
            model="external-video-v1",
        )
        capabilities: ClassVar[dict] = {
            "reference_images": {"max": 9},
            "reference_videos": {"max": 3, "total_duration_seconds": 15},
        }

    original_settings = content_main.settings
    content_main.settings = replace(
        original_settings,
        provider_media_base_url=enabled.provider_media_base_url,
        provider_media_signing_secret=enabled.provider_media_signing_secret,
        provider_media_url_ttl_seconds=enabled.provider_media_url_ttl_seconds,
    )
    content_main.app.dependency_overrides[get_ai_video_generation_port] = ExternalVideoProvider
    try:
        response = content_client.get("/api/v1/capabilities", headers=user_headers)
        assert response.status_code == 200, response.text
        video = next(
            item
            for item in response.json()["capabilities"]
            if item["capability"] == "ai-video-generation"
        )
        reference_videos = video["parameters"]["reference_videos"]
        assert reference_videos["project_asset_transport_available"] is True
        assert reference_videos["project_asset_transport"] == "provider-signed-https-gateway"
        assert reference_videos["project_asset_url_ttl_seconds"] == 600
    finally:
        content_main.settings = original_settings
        content_main.app.dependency_overrides.pop(get_ai_video_generation_port, None)
