"""Network policy for the tenant HiAgent gateway and its result downloads."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .llm_adapters import ProviderCallError


def configured_hiagent_base_url() -> str:
    """The tenant HiAgent gateway from HIAGENT_BASE_URL; empty when not configured."""
    return os.getenv("HIAGENT_BASE_URL", "").strip()


def configured_hiagent_host() -> str:
    return (urlsplit(configured_hiagent_base_url()).hostname or "").lower().rstrip(".")


_ENTERPRISE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def _download_parts(url: str):
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("invalid URL")
    if re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError("ambiguous URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("HTTPS URL without credentials required")
    # Accessing port validates malformed/out-of-range ports before any connection.
    port = parsed.port
    if port == 0:
        raise ValueError("invalid port")
    origin = (parsed.hostname.lower().rstrip("."), 443 if port is None else port)
    path = parsed.path or "/"
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if (
        unquote(path) != path
        or "\\" in path
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or re.search(r"[\x00-\x1f\x7f]", path)
    ):
        raise ValueError("ambiguous URL path")
    return parsed, origin, path


def hiagent_result_download_prefixes(base_url: str) -> tuple[str, ...]:
    """Trust configured file routes, never a host supplied by a model response.

    HiAgent's official signed-download helper uses the API hostname on HTTPS
    port 443 even when its API is served on port 30040. Both precise origins
    are accepted only at the signed download endpoint. Some tenants also serve
    generated image/video objects directly on a dedicated HTTPS port of the
    same host (HIAGENT_OBJECT_STORAGE_PORT), separate from the API and signed
    upload-download route.
    Extra storage locations require administrator-owned HTTPS directory prefixes.
    """
    try:
        parsed, _, _ = _download_parts(base_url)
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        origins = dict.fromkeys((host, parsed.netloc))
        prefixes = [
            urlunsplit(("https", origin, "/api/proxy/down", "Action=DownloadForPresign", ""))
            for origin in origins
        ]
        # Generated files on the storage port have ordinary object paths without
        # Action/Key query parameters. Trust that origin only on the configured
        # gateway host; neither other hosts nor other ports inherit this rule.
        storage_port = os.getenv("HIAGENT_OBJECT_STORAGE_PORT", "").strip()
        if storage_port:
            port = int(storage_port)
            if not 0 < port < 65536:
                raise ValueError("invalid storage port")
            prefixes.append(f"https://{host}:{port}/")
        for value in os.getenv("HIAGENT_RESULT_BASE_URLS", "").split(","):
            value = value.strip()
            if not value:
                continue
            extra, _, path = _download_parts(value)
            if extra.query or path == "/":
                raise ValueError("a scoped file directory is required")
            prefixes.append(value.rstrip("/"))
        return tuple(dict.fromkeys(prefixes))
    except ValueError:
        raise ProviderCallError("HiAgent 文件下载地址配置无效，请联系管理员") from None


def _trusted_result_url(url: str, prefixes: tuple[str, ...]) -> bool:
    parsed, origin, path = _download_parts(url)
    for prefix in prefixes:
        rule, rule_origin, rule_path = _download_parts(prefix)
        if origin != rule_origin:
            continue
        if rule.query:
            # This rule is generated internally for the official signed file
            # route, not a wildcard granting access to all gateway endpoints.
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (
                path == rule_path
                and query.get("Action") == ["DownloadForPresign"]
                and len(query.get("Key", [])) == 1
                and bool(query["Key"][0])
                and len(query.get("Path", [])) == 1
                and bool(query["Path"][0])
            ):
                return True
        elif path == rule_path or path.startswith(rule_path.rstrip("/") + "/"):
            return True
    return False


def safe_result_download_url(
    url: str, *, trusted_prefixes: tuple[str, ...] = (), media_name: str = "图片"
) -> str:
    """Allow public results, or scoped configured enterprise file services.

    Loopback, link-local (including cloud metadata), multicast, and unspecified
    addresses remain forbidden even when an administrator configured that URL.
    The caller must disable redirects so this check cannot be bypassed.
    """
    try:
        _, (host, port), _ = _download_parts(url)
        trusted = _trusted_result_url(url, trusted_prefixes)
    except ValueError:
        raise ProviderCallError(f"{media_name}结果必须使用无凭据的 HTTPS 下载地址") from None
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ProviderCallError(f"{media_name}结果地址不能指向本机或内网")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, port)}
        except (OSError, ValueError):
            raise ProviderCallError(f"{media_name}结果地址无法解析，请稍后重试") from None
    else:
        addresses = {address}
    for address in addresses:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        enterprise = any(
            address.version == network.version and address in network
            for network in _ENTERPRISE_NETWORKS
        )
        if not (address.is_global or (trusted and enterprise)) or address.is_multicast:
            raise ProviderCallError(f"{media_name}结果地址不能指向非公网 IP")
    if not addresses:
        raise ProviderCallError(f"{media_name}结果地址无法解析，请稍后重试")
    return url


def use_hiagent_env_proxy() -> bool:
    return os.getenv("HIAGENT_USE_ENV_PROXY", "").strip().lower() in {"true", "1", "yes", "on"}


def hiagent_download_httpx_options(url: str) -> dict[str, bool]:
    """Override only the known tenant host; other suppliers keep HTTPX defaults."""
    host = configured_hiagent_host()
    if host and (urlsplit(url).hostname or "").lower().rstrip(".") == host:
        return {"trust_env": use_hiagent_env_proxy()}
    return {}
