import json
from types import SimpleNamespace

import httpcore
import httpx
import pytest
import requests
from requests.utils import select_proxy

from content_service import hiagent_quickstart
from content_service.hiagent_adapters import HiAgentImageGenerationPort
from content_service.llm_adapters import ProviderCallError
from content_service.media_adapters import LocalAssetStore, download_generated_video
from content_service.providers import ImageGenerationRequest


@pytest.fixture
def captured_requests(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HIAGENT_USE_ENV_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    # Use dummy credentials so no real signing material enters test requests.
    monkeypatch.setattr(hiagent_quickstart, "ACCESS_KEY_ID", "test-ak")
    monkeypatch.setattr(hiagent_quickstart, "SECRET_KEY", "test-sk")
    monkeypatch.setattr(hiagent_quickstart, "WORKSPACE_ID", "test-workspace")
    captured = []

    def send(adapter, request, **kwargs):
        captured.append({"url": request.url, "proxies": kwargs.get("proxies", {})})
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"Result": {"ok": True}}).encode()
        response.url = request.url
        response.request = request
        return response

    # Exercise requests' real Session/environment merge; intercept only the
    # transport boundary so this regression never contacts a gateway or proxy.
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return captured


def _call_gateway(kind):
    if kind == "model":
        return hiagent_quickstart.call("ImageGen", {"Prompt": "test"}, timeout=5)
    if kind == "upload":
        return hiagent_quickstart.up_call("UploadRaw", body=b"test", timeout=5)
    return hiagent_quickstart._post_stream("Chat", {"Prompt": "test"}, timeout=5)


@pytest.mark.parametrize("kind", ["model", "upload", "stream"])
@pytest.mark.parametrize("proxy_case", ["upper", "lower", "all_only"])
@pytest.mark.parametrize("enabled", [False, True])
def test_gateway_proxy_inheritance_is_explicit(
    monkeypatch, captured_requests, kind, proxy_case, enabled
):
    proxy = "http://127.0.0.1:17897"
    schemes = ("all",) if proxy_case == "all_only" else ("http", "https", "all")
    for scheme in schemes:
        name = f"{scheme}_proxy"
        monkeypatch.setenv(name if proxy_case == "lower" else name.upper(), proxy)
    if enabled:
        monkeypatch.setenv("HIAGENT_USE_ENV_PROXY", "true")

    _call_gateway(kind)

    gateway = captured_requests[0]
    selected = select_proxy(gateway["url"], gateway["proxies"])
    assert (selected or None) == (proxy if enabled else None)
    if enabled:
        assert all(gateway["proxies"][scheme] == proxy for scheme in schemes)
    else:
        assert not any(gateway["proxies"].get(scheme) for scheme in ("http", "https", "all"))

    # Gateway routing must not mutate process-wide proxy settings or alter
    # ordinary requests, including other providers and generated media URLs.
    requests.get("https://example.invalid/media.png", timeout=5)
    ordinary = captured_requests[1]
    assert select_proxy(ordinary["url"], ordinary["proxies"]) == proxy


@pytest.mark.parametrize("kind", ["model", "upload", "stream"])
def test_gateway_false_setting_still_bypasses_system_proxy(monkeypatch, captured_requests, kind):
    proxy = "http://127.0.0.1:17897"
    monkeypatch.setenv("HIAGENT_USE_ENV_PROXY", "false")
    monkeypatch.setattr(
        requests.sessions,
        "get_environ_proxies",
        lambda *args, **kwargs: {"https": proxy, "all": proxy},
    )

    _call_gateway(kind)

    gateway = captured_requests[0]
    assert not select_proxy(gateway["url"], gateway["proxies"])


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize(
    "host",
    ["hiagent.example.com", "media.example", "hiagent.example.com.cdn.example"],
)
@pytest.mark.parametrize("enabled", [False, True])
def test_result_download_routes_only_hiagent_host_directly(
    monkeypatch, captured_requests, tmp_path, kind, host, enabled
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    if enabled:
        monkeypatch.setenv("HIAGENT_USE_ENV_PROXY", "true")
    url = f"https://{host}/result.{'png' if kind == 'image' else 'mp4'}"
    media = b"test-result-bytes"
    routes = []

    def handle_request(transport, request):
        # Preserve HTTPX's client construction, environment merge and URL
        # routing. Stop at the network transport before opening any socket.
        routes.append(isinstance(transport._pool, httpcore.HTTPProxy))
        return httpx.Response(
            200,
            headers={"content-type": "image/png" if kind == "image" else "video/mp4"},
            content=media,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    if kind == "image":
        client = SimpleNamespace(
            workspace_id="test-workspace",
            _request=lambda *args, **kwargs: {"Data": [{"Url": url}]},
        )
        port = HiAgentImageGenerationPort(client=client, model_service_id="test-image")
        assets = port.generate(ImageGenerationRequest(prompt="test"))
        assert assets[0].content == media
    else:
        store = LocalAssetStore(tmp_path / "objects")
        result = download_generated_video(url, asset_store=store, storage_key="test.mp4")
        assert store.read_bytes(result.key) == media

    assert routes == [enabled or host != "hiagent.example.com"]


@pytest.mark.parametrize(
    ("status", "content_type", "content", "message"),
    [
        (200, "text/html", b"error", "Content-Type"),
        (200, "video/mp4", b"", "为空"),
        (200, "video/mp4", b"too-large", "超过允许大小"),
        (302, "video/mp4", b"test", "ai-video-download"),
    ],
)
def test_direct_hiagent_video_download_keeps_response_guards(
    monkeypatch, captured_requests, tmp_path, status, content_type, content, message
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17897")
    requested = []

    def handle_request(transport, request):
        requested.append(str(request.url))
        return httpx.Response(
            status,
            headers={"content-type": content_type, "location": "https://other.example/file.mp4"},
            content=content,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    url = "https://hiagent.example.com/result.mp4"
    with pytest.raises(ProviderCallError, match=message):
        download_generated_video(
            url,
            asset_store=LocalAssetStore(tmp_path / "objects"),
            storage_key="rejected.mp4",
            max_bytes=4,
        )
    assert requested == [url]
