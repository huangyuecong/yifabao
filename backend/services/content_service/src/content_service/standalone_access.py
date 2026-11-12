"""Explicit local-only workbench access, separate from enterprise authentication."""

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from agent_core.identity import Identity, identity_mode


@dataclass(frozen=True, slots=True)
class StandaloneIdentity(Identity):
    """Issued only after the local request boundary has been verified."""


def standalone_enabled() -> bool:
    return (
        os.getenv("CONTENT_STANDALONE_MODE", "false").strip().lower() == "true"
        and identity_mode() == "development"
    )


def _loopback(value: str | None) -> bool:
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def _local_host(value: str | None) -> bool:
    return value == "localhost" or _loopback(value)


def require_local_request(request: Request | None) -> None:
    # Do not trust X-Forwarded-* here. The standalone launcher disables Uvicorn's
    # proxy-header processing and binds both the API and Vite to loopback.
    if request is None or request.client is None or not _loopback(request.client.host):
        raise HTTPException(403, "本机工作台仅允许从本机访问", headers={"Cache-Control": "private, no-store"})
    try:
        allowed = _local_host(request.url.hostname)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            allowed = allowed and parsed.scheme in {"http", "https"} and _local_host(parsed.hostname) and not parsed.username and not parsed.password
        elif request.headers.get("sec-fetch-site") == "cross-site":
            allowed = False
    except ValueError:
        allowed = False
    if not allowed:
        raise HTTPException(403, "本机工作台不接受外部站点的请求", headers={"Cache-Control": "private, no-store"})


def local_identity(request: Request | None, permissions: frozenset[str]) -> StandaloneIdentity:
    require_local_request(request)
    return StandaloneIdentity(
        user_id="dev-user", name="本机用户", department="", roles=frozenset({"user", "reviewer", "admin"}),
        permissions=permissions,
    )
