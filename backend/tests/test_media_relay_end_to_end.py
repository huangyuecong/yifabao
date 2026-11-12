"""Real loopback HTTP tests of the client/server contract, without cloud calls.

The test transport alone maps an HTTPS origin onto the local HTTP listener;
production clients continue to require normal verified HTTPS.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import httpx
import pytest

from content_service import main as content_main
from content_service.http_media_relay import HttpMediaRelayPublisher
from content_service.provider_dependencies import (
    get_ai_video_generation_port,
    get_provider_media_publisher,
)
from content_service.providers import ProviderStatus

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relay = load_module("test_relay_service_impl", ROOT / "services/media_relay/service.py")
setup = load_module("test_relay_setup_impl", ROOT / "scripts/configure_media_relay.py")


@pytest.fixture
def loopback_relay(tmp_path):
    config = relay.RelayConfig(
        data_dir=tmp_path / "remote",
        public_base_url="https://203.0.113.10:8443",
        upload_token="test_only_token_" + "x" * 40,
        signing_secret=b"test_only_" + b"s" * 40,
    )
    server = relay.create_server(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_client = httpx.Client
    endpoint = f"http://127.0.0.1:{server.server_port}"

    def forward(request):
        assert request.url.host == "203.0.113.10" and request.url.port == 8443
        path = request.url.raw_path.decode("ascii")
        headers = dict(request.headers)
        headers.pop("host", None)
        with actual_client(timeout=5, trust_env=False) as client:
            response = client.request(
                request.method, endpoint + path, content=request.read(), headers=headers
            )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    transport = httpx.MockTransport(forward)
    try:
        yield config, transport, actual_client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_publisher_upload_download_range_and_delete(loopback_relay, tmp_path):
    config, transport, client_class = loopback_relay
    objects = tmp_path / "local"
    objects.mkdir()
    video = objects / "reference.mp4"
    video.write_bytes(setup.PROBE)
    publisher = HttpMediaRelayPublisher(
        base_url=config.public_base_url,
        token=config.upload_token,
        url_ttl_seconds=1800,
        allowed_root=objects,
        http_transport=transport,
    )
    publication = publisher.publish(video, media_type="video/mp4", publication_id="local-test")
    assert publication.transport == "private-relay-presigned-https"
    with client_class(transport=transport) as client:
        assert client.head(publication.url).status_code == 200
        response = client.get(publication.url, headers={"Range": "bytes=0-1"})
        assert response.status_code == 206
        assert response.content == setup.PROBE[:2]
        unsigned = config.public_base_url + urlsplit(publication.url).path
        assert client.get(unsigned).status_code in (401, 403, 404)
        publisher.release(publication.cleanup_token)
        assert client.get(publication.url).status_code == 404
    assert publisher.purge_expired() == 0


def test_setup_probe_exercises_server_and_cleans_object(loopback_relay, monkeypatch):
    config, transport, client_class = loopback_relay

    def client_factory(**kwargs):
        assert kwargs.get("follow_redirects") is False
        assert kwargs.get("trust_env") is False
        kwargs["transport"] = transport
        return client_class(**kwargs)

    monkeypatch.setattr(setup.httpx, "Client", client_factory)
    setup.probe_connection(config.public_base_url, config.upload_token)
    assert not list(config.data_dir.glob("*.media"))


def test_save_settings_keeps_existing_configuration_and_private_backup(tmp_path):
    env_file = tmp_path / ".env"
    original = "CONTENT_LLM_PROVIDER=existing\nUNRELATED=value\n"
    env_file.write_text(original, encoding="utf-8")
    token = "test_only_token_" + "x" * 40
    setup.save_settings(env_file, "https://203.0.113.10:8443", token, 1800)
    text = env_file.read_text(encoding="utf-8")
    assert original in text
    assert token not in text
    assert "CONTENT_PROVIDER_MEDIA_RELAY_BASE_URL=https://203.0.113.10:8443" in text
    secret = next((tmp_path / ".data/secrets").iterdir())
    assert secret.read_text() == token
    backup = next((tmp_path / ".data/config-backups").iterdir())
    assert backup.read_text() == original


def test_setup_rejects_existing_tos_before_mutation(tmp_path):
    env_file = tmp_path / ".env"
    original = "CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT=https://tos.example.com\n"
    env_file.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="TOS"):
        setup.save_settings(env_file, "https://203.0.113.10:8443", "x" * 40, 1800)
    assert env_file.read_text() == original
    assert not (tmp_path / ".data").exists()


def test_capabilities_enable_private_relay_reference_videos(content_client, user_headers, tmp_path):
    class ExternalVideo:
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

    publisher = HttpMediaRelayPublisher(
        base_url="https://203.0.113.10:8443",
        token="test_only_token_" + "x" * 40,
        url_ttl_seconds=1800,
        allowed_root=tmp_path,
    )
    overrides = content_main.app.dependency_overrides
    previous = overrides.copy()
    overrides[get_ai_video_generation_port] = ExternalVideo
    overrides[get_provider_media_publisher] = lambda: publisher
    try:
        response = content_client.get("/api/v1/capabilities", headers=user_headers)
        assert response.status_code == 200
        document = next(
            x for x in response.json()["capabilities"] if x["capability"] == "ai-video-generation"
        )
        references = document["parameters"]["reference_videos"]
        assert references["project_asset_transport_available"] is True
        assert references["project_asset_transport"] == "private-relay-presigned-https"
        assert references["private_temporary_object"] is True
        assert references["max"] == 3 and references["total_duration_seconds"] == 15
    finally:
        overrides.clear()
        overrides.update(previous)
