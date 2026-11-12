from __future__ import annotations

import logging
import os
import traceback
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from content_service.config import load_settings
from content_service.http_media_relay import HttpMediaRelayPublisher
from content_service.provider_media_publishers import ProviderMediaPublicationError

TOKEN = "private-upload-token-" + "a" * 32
SIGNATURE = "f" * 64
OBJECT_ID = "b" * 32
ORIGIN = "https://203.0.113.10"
NOW = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)
EXPIRES = int(NOW.timestamp()) + 1800
MP4 = b"\x00\x00\x00\x18ftypisom" + b"private-reference-video"


@pytest.fixture(autouse=True)
def isolated_media_settings(monkeypatch):
    for name in os.environ:
        if name.startswith("CONTENT_PROVIDER_MEDIA_"):
            monkeypatch.delenv(name, raising=False)


def payload(**changes):
    return {
        "url": f"{ORIGIN}/v1/media/{OBJECT_ID}?expires={EXPIRES}&signature={SIGNATURE}",
        "cleanup_token": OBJECT_ID,
        "expires_at": EXPIRES,
        **changes,
    }


def publisher(tmp_path, handler, **changes):
    return HttpMediaRelayPublisher(
        **{
            "base_url": ORIGIN,
            "token": TOKEN,
            "url_ttl_seconds": 1800,
            "allowed_root": tmp_path / "objects",
            "clock": lambda: NOW,
            "http_transport": httpx.MockTransport(handler),
            **changes,
        }
    )


def source(tmp_path, body=MP4):
    path = tmp_path / "objects" / "opaque-object"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def publish(item, path):
    return item.publish(path, media_type="video/mp4", publication_id="internal-job:asset")


def test_relay_streams_video_and_releases_only_on_fixed_origin(tmp_path, caplog):
    calls = []
    video = MP4 + b"m" * 200_000
    path = source(tmp_path, video)

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.url.scheme == "https"
        assert request.url.host == "203.0.113.10"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.extensions["timeout"] == {
            "connect": 10,
            "read": 60,
            "write": 120,
            "pool": 10,
        }
        if request.url.path == "/v1/objects":
            assert request.headers["Content-Type"] == "video/mp4"
            assert request.headers["Content-Length"] == str(len(video))
            assert "transfer-encoding" not in request.headers
            assert request.read() == video
            assert "internal-job" not in str(request.url)
            return httpx.Response(201, json=payload())
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"deleted": 2})

    item = publisher(tmp_path, handle)
    with caplog.at_level(logging.DEBUG):
        publication = publish(item, path)
        item.release(publication.cleanup_token)
        item.release(publication.cleanup_token)
        assert item.purge_expired() == 2

    assert publication.url == payload()["url"]
    assert publication.cleanup_token == OBJECT_ID
    assert publication.expires_at == datetime.fromtimestamp(EXPIRES, tz=UTC)
    assert publication.transport == "private-relay-presigned-https"
    assert item.enabled is True
    assert item.url_ttl_seconds == 1800
    assert calls == [
        ("POST", "/v1/objects"),
        ("DELETE", f"/v1/objects/{OBJECT_ID}"),
        ("DELETE", f"/v1/objects/{OBJECT_ID}"),
        ("POST", "/v1/cleanup"),
    ]
    assert TOKEN not in repr(item)
    assert TOKEN not in caplog.text
    assert SIGNATURE not in caplog.text


def test_relay_upload_is_streamed_in_bounded_chunks(tmp_path):
    chunks = []

    class RecordingTransport(httpx.BaseTransport):
        def handle_request(self, request):
            chunks.extend(len(chunk) for chunk in request.stream)
            return httpx.Response(201, json=payload())

    path = source(tmp_path, MP4 + b"v" * 250_000)
    item = publisher(tmp_path, None, http_transport=RecordingTransport())
    publish(item, path)
    assert len(chunks) > 1
    assert max(chunks) <= 64 * 1024
    assert sum(chunks) == path.stat().st_size


def test_relay_accepts_configured_https_port_and_quicktime_video(tmp_path):
    origin = f"{ORIGIN}:8443"

    def handle(request):
        assert request.url.port == 8443
        assert request.headers["Content-Type"] == "video/quicktime"
        return httpx.Response(201, json=payload(url=payload()["url"].replace(ORIGIN, origin)))

    item = publisher(tmp_path, handle, base_url=origin)
    publication = item.publish(
        source(tmp_path, b"\x00\x00\x00\x18ftypqt  " + b"private-video"),
        media_type="video/quicktime",
        publication_id="job",
    )
    assert publication.url.startswith(origin + "/v1/media/")


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 413, 500])
def test_relay_rejects_errors_and_redirects_without_leaking_body(tmp_path, status, caplog):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://untrusted.example/upload"},
            text=f"{TOKEN} {SIGNATURE}",
        )

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderMediaPublicationError) as error:
        publish(publisher(tmp_path, handle), source(tmp_path))
    assert len(requests) == 1
    displayed = "".join(traceback.format_exception(error.value)) + caplog.text
    assert TOKEN not in displayed
    assert SIGNATURE not in displayed
    assert str(status) in str(error.value)


def test_relay_transport_error_does_not_expose_original_exception(tmp_path):
    def handle(request):
        raise httpx.ConnectError(f"transport details {TOKEN} {SIGNATURE}", request=request)

    with pytest.raises(ProviderMediaPublicationError) as error:
        publish(publisher(tmp_path, handle), source(tmp_path))
    displayed = "".join(traceback.format_exception(error.value))
    assert TOKEN not in displayed
    assert SIGNATURE not in displayed


@pytest.mark.parametrize(
    "changes",
    [
        {"url": payload()["url"].replace(ORIGIN, "https://attacker.example")},
        {"url": payload()["url"].replace(ORIGIN, "http://203.0.113.10")},
        {"url": payload()["url"].replace(ORIGIN, f"{ORIGIN}:444")},
        {"url": payload()["url"].replace(ORIGIN, "https://user:password@203.0.113.10")},
        {"url": payload()["url"].replace(OBJECT_ID, "c" * 32)},
        {"url": payload()["url"] + "&expires=1"},
        {"url": payload()["url"] + "&other=value"},
        {"url": payload()["url"] + "#"},
        {"url": payload()["url"].replace(SIGNATURE, "bad-signature")},
        {"url": payload()["url"].replace(str(EXPIRES), "0")},
        {"expires_at": int(NOW.timestamp()) - 1},
        {"expires_at": EXPIRES + 31},
        {"expires_at": str(EXPIRES)},
        {"expires_at": True},
        {"url": None},
    ],
)
def test_relay_rejects_untrusted_publications_and_cleans_up(tmp_path, changes):
    calls = []

    def handle(request):
        calls.append((request.method, str(request.url)))
        if request.method == "DELETE":
            return httpx.Response(204)
        result = payload(**changes)
        # Independently test the expiry bound instead of only a mismatch.
        if type(changes.get("expires_at")) is int:
            result["url"] = payload()["url"].replace(str(EXPIRES), str(changes["expires_at"]))
        return httpx.Response(201, json=result)

    with pytest.raises(ProviderMediaPublicationError, match="短时签名地址"):
        publish(publisher(tmp_path, handle), source(tmp_path))
    assert calls == [
        ("POST", f"{ORIGIN}/v1/objects"),
        ("DELETE", f"{ORIGIN}/v1/objects/{OBJECT_ID}"),
    ]


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"secret":"' + b"s" * 20000 + b'"}'])
def test_relay_rejects_malformed_or_oversized_response(tmp_path, body):
    def handle(request):
        return httpx.Response(201, content=body)

    with pytest.raises(ProviderMediaPublicationError):
        publish(publisher(tmp_path, handle), source(tmp_path))


@pytest.mark.parametrize(
    "cleanup_token", ["../outside", "https://elsewhere.example", "B" * 32, None]
)
def test_relay_rejects_invalid_cleanup_tokens(tmp_path, cleanup_token):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(201, json=payload(cleanup_token=cleanup_token))

    item = publisher(tmp_path, handle)
    with pytest.raises(ProviderMediaPublicationError, match="清理标识无效"):
        item.release(cleanup_token)
    assert not calls
    with pytest.raises(ProviderMediaPublicationError, match="清理标识无效"):
        publish(item, source(tmp_path))
    assert len(calls) == 1


@pytest.mark.parametrize("deleted", [-1, True, "2", None])
def test_relay_rejects_invalid_cleanup_counts(tmp_path, deleted):
    item = publisher(tmp_path, lambda request: httpx.Response(200, json={"deleted": deleted}))
    with pytest.raises(ProviderMediaPublicationError, match="清理响应无效"):
        item.purge_expired()


def test_relay_rejects_outside_root_bad_type_bad_magic_empty_and_oversized_source(tmp_path):
    calls = []
    item = publisher(tmp_path, lambda request: calls.append(request))
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(MP4)
    with pytest.raises(ProviderMediaPublicationError, match="不在内容素材目录"):
        publish(item, outside)
    path = source(tmp_path)
    with pytest.raises(ProviderMediaPublicationError, match="仅允许 MP4 或 MOV"):
        item.publish(path, media_type="image/png", publication_id="job")
    path.write_bytes(b"this is not video")
    with pytest.raises(ProviderMediaPublicationError, match="格式校验失败"):
        publish(item, path)
    path.write_bytes(b"")
    with pytest.raises(ProviderMediaPublicationError, match="200MB"):
        publish(item, path)
    with path.open("wb") as stream:
        stream.truncate(200 * 1024 * 1024 + 1)
    with pytest.raises(ProviderMediaPublicationError, match="200MB"):
        publish(item, path)
    assert not calls


@pytest.mark.parametrize(
    "base_url",
    [
        "http://203.0.113.10",
        "https://203.0.113.10/path",
        "https://203.0.113.10//",
        "https://user:password@203.0.113.10",
        "https://203.0.113.10?",
        "https://203.0.113.10#",
        "https://203.0.113.10?foo=bar",
        "https://203.0.113.10:0",
        "https://203.0.113.10:99999",
        "https://203.0.113.10\n",
        "https://203.0.113.10\\@attacker.example",
    ],
)
def test_relay_rejects_invalid_origins_before_network(tmp_path, base_url):
    with pytest.raises(ValueError, match="HTTPS 地址"):
        publisher(tmp_path, None, base_url=base_url)


def test_relay_settings_are_complete_and_keep_secret_out_of_repr(monkeypatch, tmp_path):
    disabled = load_settings()
    assert disabled.provider_media_relay_base_url is None
    assert disabled.provider_media_relay_token is None
    assert disabled.provider_media_relay_url_ttl_seconds == 1800
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_RELAY_BASE_URL", ORIGIN + "/")
    with pytest.raises(ValueError, match="配置不完整"):
        load_settings()
    token_path = tmp_path / "relay-token"
    token_path.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_RELAY_TOKEN_FILE", str(token_path))
    settings = load_settings()
    assert settings.provider_media_relay_base_url == ORIGIN
    assert settings.provider_media_relay_token == TOKEN
    assert TOKEN not in repr(settings)
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_RELAY_TOKEN", TOKEN)
    with pytest.raises(ValueError, match="只能配置一种"):
        load_settings()


@pytest.mark.parametrize("ttl", ["0", "59", "3601"])
def test_relay_settings_reject_invalid_ttl(monkeypatch, ttl):
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_RELAY_URL_TTL_SECONDS", ttl)
    with pytest.raises(ValueError, match="60 到 3600"):
        load_settings()


def test_relay_settings_reject_invalid_tokens_and_tos_conflict(monkeypatch):
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_RELAY_BASE_URL", ORIGIN)
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_RELAY_TOKEN", "short-secret")
    with pytest.raises(ValueError, match="安全令牌") as error:
        load_settings()
    assert "short-secret" not in str(error.value)
    monkeypatch.setenv("CONTENT_PROVIDER_MEDIA_RELAY_TOKEN", TOKEN)
    for name, value in {
        "ENDPOINT": "https://tos.example",
        "REGION": "cn-test",
        "BUCKET": "private-bucket",
        "ACCESS_KEY": "private-access-key",
        "SECRET_KEY": "private-secret-key",
    }.items():
        monkeypatch.setenv(f"CONTENT_PROVIDER_MEDIA_TOS_{name}", value)
    with pytest.raises(ValueError, match="只能配置一种"):
        load_settings()


def test_dependency_selects_relay_or_disabled_and_checks_manual_settings(monkeypatch):
    from content_service import provider_dependencies as dependencies

    initial = load_settings()
    dependencies.get_provider_media_publisher.cache_clear()
    monkeypatch.setattr(dependencies, "get_runtime_settings", lambda: initial)
    try:
        assert dependencies.get_provider_media_publisher().enabled is False
        relay = replace(
            initial, provider_media_relay_base_url=ORIGIN, provider_media_relay_token=TOKEN
        )
        dependencies.get_provider_media_publisher.cache_clear()
        monkeypatch.setattr(dependencies, "get_runtime_settings", lambda: relay)
        assert isinstance(dependencies.get_provider_media_publisher(), HttpMediaRelayPublisher)
        for invalid in (
            replace(relay, provider_media_relay_token=None),
            replace(relay, provider_media_relay_base_url=None),
            replace(relay, provider_media_tos_endpoint="https://tos.example"),
            replace(
                relay,
                provider_media_tos_endpoint="https://tos.example",
                provider_media_relay_base_url=None,
            ),
        ):
            dependencies.get_provider_media_publisher.cache_clear()
            monkeypatch.setattr(dependencies, "get_runtime_settings", lambda value=invalid: value)
            with pytest.raises(RuntimeError):
                dependencies.get_provider_media_publisher()
    finally:
        dependencies.get_provider_media_publisher.cache_clear()
