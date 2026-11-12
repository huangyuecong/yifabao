"""HiAgent signed downloads can use the enterprise network, with scoped trust."""

import socket

import httpx
import pytest

from content_service.chat_media import _download_image
from content_service.hiagent_network import (
    hiagent_result_download_prefixes,
    safe_result_download_url,
)
from content_service.llm_adapters import ProviderCallError
from content_service.media_adapters import LocalAssetStore, download_generated_video

GATEWAY = "https://hiagent.example.com:30040"
DOWNLOAD = (
    "https://hiagent.example.com/api/proxy/down"
    "?Action=DownloadForPresign&Key=dummy&Path=generated%2Ffile"
)
OBJECT_DOWNLOAD = "https://hiagent.example.com:30873/bucket/2026/10/result.mp4"


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    monkeypatch.delenv("HIAGENT_RESULT_BASE_URLS", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.2.99", 443))],
    )


@pytest.mark.parametrize("port", ["", ":443", ":30040"])
def test_signed_download_accepts_exact_configured_file_origins(port):
    url = DOWNLOAD.replace("hiagent.example.com", "hiagent.example.com" + port)
    assert safe_result_download_url(
        url, trusted_prefixes=hiagent_result_download_prefixes(GATEWAY)
    ) == url


@pytest.mark.parametrize("extension", ["png", "jpg", "mp4"])
def test_tenant_generated_object_storage_on_actual_port(extension):
    url = OBJECT_DOWNLOAD.rsplit(".", 1)[0] + "." + extension
    assert safe_result_download_url(
        url, trusted_prefixes=hiagent_result_download_prefixes(GATEWAY)
    ) == url


@pytest.mark.parametrize("url", [
    OBJECT_DOWNLOAD.replace(":30873", ":30874"),
    OBJECT_DOWNLOAD.replace(":30873", ":30040"),
    OBJECT_DOWNLOAD.replace(":30873", ""),
    OBJECT_DOWNLOAD.replace("hiagent.example.com", "hiagent.example.com.attacker.example"),
    OBJECT_DOWNLOAD.replace("hiagent.example.com", "other.example"),
    OBJECT_DOWNLOAD.replace("hiagent.example.com", "10.0.2.19"),
    OBJECT_DOWNLOAD.replace("https://", "http://"),
])
def test_generated_storage_permission_does_not_extend_to_other_origins(url):
    with pytest.raises(ProviderCallError):
        safe_result_download_url(url, trusted_prefixes=hiagent_result_download_prefixes(GATEWAY))


def test_other_tenant_does_not_inherit_yifabao_object_storage():
    with pytest.raises(ProviderCallError):
        safe_result_download_url(
            OBJECT_DOWNLOAD,
            trusted_prefixes=hiagent_result_download_prefixes("https://other.example:30040"),
        )


@pytest.mark.parametrize(
    "url",
    [
        DOWNLOAD.replace("hiagent.example.com", "hiagent.example.com.attacker.example"),
        DOWNLOAD.replace("hiagent.example.com", "other.example"),
        DOWNLOAD.replace("hiagent.example.com", "hiagent.example.com:8443"),
        DOWNLOAD.replace("hiagent.example.com", "hiagent.example.com:0"),
        DOWNLOAD.replace("hiagent.example.com", "hiagent.example.com:65536"),
        DOWNLOAD.replace("/api/proxy/down", "/api/proxy/down/other"),
        DOWNLOAD.replace("/api/proxy/down", "/admin"),
        DOWNLOAD.replace("DownloadForPresign", "DeleteFile"),
        DOWNLOAD + "&Action=DeleteFile",
        DOWNLOAD.replace("&Key=dummy", ""),
        DOWNLOAD.replace("&Path=generated%2Ffile", ""),
        DOWNLOAD.replace("https://", "http://"),
        DOWNLOAD.replace("https://", "https://name:pass@"),
        DOWNLOAD + "#hidden",
        DOWNLOAD.replace("/api/proxy/down", "/api/proxy/../down"),
        DOWNLOAD.replace("/api/proxy/down", "/api/proxy/%252e%252e/down"),
        DOWNLOAD.replace("/api/proxy/down", "/api//proxy/down"),
        DOWNLOAD.replace("/api/proxy/down", "/api\\proxy/down"),
    ],
)
def test_private_download_does_not_escape_configured_origin_and_route(url):
    with pytest.raises(ProviderCallError):
        safe_result_download_url(url, trusted_prefixes=hiagent_result_download_prefixes(GATEWAY))


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["::1"],
        ["169.254.169.254"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["240.0.0.1"],
        ["100.100.100.200"],
        ["fe80::1"],
        ["::ffff:127.0.0.1"],
        ["10.0.2.99", "169.254.169.254"],
    ],
)
@pytest.mark.parametrize("url", [DOWNLOAD, OBJECT_DOWNLOAD])
def test_even_trusted_download_cannot_reach_metadata_or_loopback(addresses, monkeypatch, url):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443)) for value in addresses],
    )
    with pytest.raises(ProviderCallError, match="非公网"):
        safe_result_download_url(
            url, trusted_prefixes=hiagent_result_download_prefixes(GATEWAY)
        )


def test_additional_storage_requires_configured_path_boundary(monkeypatch):
    monkeypatch.setenv("HIAGENT_RESULT_BASE_URLS", "https://files.enterprise.example:9443/results/")
    prefixes = hiagent_result_download_prefixes(GATEWAY)
    accepted = "https://files.enterprise.example:9443/results/a.png?signature=dummy"
    assert safe_result_download_url(accepted, trusted_prefixes=prefixes) == accepted
    for url in (
        accepted.replace("/results/", "/results-other/"),
        accepted.replace(":9443", ""),
        accepted.replace("/results/", "/admin/"),
        accepted.replace("/results/", "/results/%2e%2e/admin/"),
    ):
        with pytest.raises(ProviderCallError):
            safe_result_download_url(url, trusted_prefixes=prefixes)


@pytest.mark.parametrize("trusted", [False, True])
def test_encoded_space_in_result_filename_remains_supported(trusted, monkeypatch):
    monkeypatch.setenv("HIAGENT_RESULT_BASE_URLS", "https://files.example/results/")
    if not trusted:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        )
    prefixes = hiagent_result_download_prefixes(GATEWAY) if trusted else ()
    url = "https://files.example/results/my%20picture.png"
    assert safe_result_download_url(url, trusted_prefixes=prefixes) == url


@pytest.mark.parametrize("filename", ["my picture.png", "my%00picture.png", "my%1fpicture.png", "my%7fpicture.png"])
def test_raw_space_and_encoded_controls_still_rejected(filename, monkeypatch):
    monkeypatch.setenv("HIAGENT_RESULT_BASE_URLS", "https://files.example/results/")
    with pytest.raises(ProviderCallError):
        safe_result_download_url(
            "https://files.example/results/" + filename,
            trusted_prefixes=hiagent_result_download_prefixes(GATEWAY),
        )


@pytest.mark.parametrize(
    "prefix",
    ["https://files.example/", "http://files.example/results", "https://files.example/results?x=1"],
)
def test_extra_storage_config_must_be_https_with_scoped_directory(prefix, monkeypatch):
    monkeypatch.setenv("HIAGENT_RESULT_BASE_URLS", prefix)
    with pytest.raises(ProviderCallError, match="配置无效"):
        hiagent_result_download_prefixes(GATEWAY)


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("url", [DOWNLOAD, OBJECT_DOWNLOAD])
def test_trusted_download_redirect_never_inherits_private_network_access(kind, monkeypatch, tmp_path, url):
    calls = []

    def response(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://169.254.169.254/metadata"})

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(response)),
    )
    prefixes = hiagent_result_download_prefixes(GATEWAY)
    with pytest.raises(ProviderCallError):
        if kind == "image":
            _download_image(url, timeout_seconds=1, trusted_download_prefixes=prefixes)
        else:
            download_generated_video(
                url,
                asset_store=LocalAssetStore(tmp_path),
                storage_key="result.mp4",
                trusted_download_prefixes=prefixes,
            )
    assert calls == [url]
