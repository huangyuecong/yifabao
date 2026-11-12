from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, urlsplit

import httpx

from .media_ingest import (
    MediaIngestPolicy,
    MediaValidationError,
    read_upload_head,
    validate_upload_header,
)
from .provider_media_publishers import ProviderMediaPublicationError
from .providers import ProviderMediaPublication

_MAX_REFERENCE_VIDEO_BYTES = 200 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024
_VIDEO_EXTENSIONS = {"video/mp4": ".mp4", "video/quicktime": ".mov"}
_OBJECT_ID = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+={0,2}")


def validate_relay_base_url(value: str) -> str:
    """Keep authenticated requests on one explicitly configured HTTPS origin."""
    try:
        parsed = urlsplit(value)
        invalid = (
            not value
            or re.search(r"[\x00-\x20\x7f\\?#]", value)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.port == 0
        )
        # Validate IDNA, IPv6 and malformed port syntax using the request library too.
        normalized = str(httpx.URL(value)).rstrip("/")
    except (ValueError, httpx.InvalidURL):
        invalid = True
        normalized = ""
    if invalid:
        raise ValueError(
            "CONTENT_PROVIDER_MEDIA_RELAY_BASE_URL 必须是无路径、无凭据、"
            "无查询串、无片段的 HTTPS 地址"
        ) from None
    return normalized


def validate_relay_token(value: str) -> str:
    if not 32 <= len(value) <= 512 or not _TOKEN.fullmatch(value):
        raise ValueError("CONTENT_PROVIDER_MEDIA_RELAY_TOKEN 必须是 32 到 512 字符的安全令牌")
    return value


class HttpMediaRelayPublisher:
    """Stream already-ingested reference videos to a private HTTPS relay.

    Upload credentials never travel to signed download URLs. Redirects are always
    rejected, TLS certificate verification stays enabled, and response/error
    bodies are never included in exceptions or application logs.
    """

    enabled = True
    transport = "private-relay-presigned-https"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        url_ttl_seconds: int,
        allowed_root: Path,
        http_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = validate_relay_base_url(base_url)
        self._token = validate_relay_token(token)
        self.url_ttl_seconds = int(url_ttl_seconds)
        if not 60 <= self.url_ttl_seconds <= 3600:
            raise ValueError("CONTENT_PROVIDER_MEDIA_RELAY_URL_TTL_SECONDS 必须在 60 到 3600 之间")
        self.allowed_root = allowed_root.expanduser().resolve()
        self._http_transport = http_transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @contextmanager
    def _validated_source(
        self, source_path: Path, media_type: str
    ) -> Iterator[tuple[BinaryIO, int]]:
        try:
            path = source_path.expanduser().resolve()
            path.relative_to(self.allowed_root)
        except (OSError, ValueError, RuntimeError):
            raise ProviderMediaPublicationError("临时发布源文件不在内容素材目录内") from None
        if media_type not in _VIDEO_EXTENSIONS:
            raise ProviderMediaPublicationError("临时发布仅允许 MP4 或 MOV 参考视频")
        try:
            if not path.is_file():
                raise ProviderMediaPublicationError("参考视频文件不可用")
            with path.open("rb") as source:
                source_stat = os.fstat(source.fileno())
                size_bytes = source_stat.st_size
                if not stat.S_ISREG(source_stat.st_mode):
                    raise ProviderMediaPublicationError("参考视频文件不可用")
                if size_bytes < 1 or size_bytes > _MAX_REFERENCE_VIDEO_BYTES:
                    raise ProviderMediaPublicationError("参考视频大小必须在 1 字节到 200MB 之间")
                try:
                    kind, canonical = validate_upload_header(
                        filename=f"reference{_VIDEO_EXTENSIONS[media_type]}",
                        declared_media_type=media_type,
                        head=read_upload_head(source),
                        size_bytes=size_bytes,
                        policy=MediaIngestPolicy(),
                    )
                except MediaValidationError:
                    raise ProviderMediaPublicationError("参考视频文件格式校验失败") from None
                if kind != "video" or canonical != media_type:
                    raise ProviderMediaPublicationError("临时发布仅允许 MP4 或 MOV 参考视频")
                yield source, size_bytes
        except OSError:
            raise ProviderMediaPublicationError("参考视频文件不可用") from None

    @staticmethod
    def _chunks(source: BinaryIO, size_bytes: int) -> Iterator[bytes]:
        remaining = size_bytes
        while remaining:
            block = source.read(min(64 * 1024, remaining))
            if not block:
                raise ProviderMediaPublicationError("参考视频在上传过程中发生变化")
            remaining -= len(block)
            yield block

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        source: BinaryIO | None = None,
        size_bytes: int = 0,
        media_type: str | None = None,
    ) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if source is not None:
            headers.update({"Content-Length": str(size_bytes), "Content-Type": media_type or ""})
        try:
            with (
                httpx.Client(
                    verify=True,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(connect=10, read=60, write=120, pool=10),
                    transport=self._http_transport,
                ) as client,
                client.stream(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    content=self._chunks(source, size_bytes) if source is not None else b"",
                ) as response,
            ):
                if response.status_code != expected_status:
                    raise ProviderMediaPublicationError(
                        f"HTTPS 素材中转请求失败（HTTP {response.status_code}）"
                    )
                if expected_status == 204:
                    return {}
                body = bytearray()
                for block in response.iter_bytes(chunk_size=4096):
                    body.extend(block)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ProviderMediaPublicationError("HTTPS 素材中转响应过大")
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ProviderMediaPublicationError("HTTPS 素材中转响应格式无效")
                return payload
        except ProviderMediaPublicationError:
            raise
        except (httpx.HTTPError, ValueError, OSError):
            raise ProviderMediaPublicationError("HTTPS 素材中转请求失败") from None

    @staticmethod
    def _cleanup_token(value: object) -> str:
        if not isinstance(value, str) or not _OBJECT_ID.fullmatch(value):
            raise ProviderMediaPublicationError("HTTPS 素材中转清理标识无效")
        return value

    def _publication(
        self, payload: dict[str, object], cleanup_token: str
    ) -> ProviderMediaPublication:
        url = payload.get("url")
        expiry = payload.get("expires_at")
        try:
            if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f\\#]", url):
                raise ValueError
            parsed = urlsplit(url)
            configured = urlsplit(self.base_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != configured.hostname
                or (parsed.port or 443) != (configured.port or 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path != f"/v1/media/{cleanup_token}"
            ):
                raise ValueError
            params = parse_qs(parsed.query, strict_parsing=True)
            now_epoch = self._now().timestamp()
            if (
                set(params) != {"expires", "signature"}
                or len(params["expires"]) != 1
                or len(params["signature"]) != 1
                or not re.fullmatch(r"[0-9a-f]{64}", params["signature"][0])
                or type(expiry) is not int
                or params["expires"][0] != str(expiry)
                or expiry <= now_epoch
                or expiry > now_epoch + self.url_ttl_seconds + 30
            ):
                raise ValueError
            expires_at = datetime.fromtimestamp(expiry, tz=UTC)
        except (ValueError, OverflowError, OSError, TypeError):
            raise ProviderMediaPublicationError("HTTPS 素材中转未返回有效的短时签名地址") from None
        return ProviderMediaPublication(
            url=url,
            transport=self.transport,
            expires_at=expires_at,
            cleanup_token=cleanup_token,
        )

    def publish(
        self, source_path: Path, *, media_type: str, publication_id: str
    ) -> ProviderMediaPublication:
        del publication_id
        with self._validated_source(source_path, media_type) as (source, size_bytes):
            payload = self._request(
                "POST",
                "/v1/objects",
                expected_status=201,
                source=source,
                size_bytes=size_bytes,
                media_type=media_type,
            )
        cleanup_token = self._cleanup_token(payload.get("cleanup_token"))
        try:
            return self._publication(payload, cleanup_token)
        except ProviderMediaPublicationError:
            try:  # noqa: SIM105
                self.release(cleanup_token)
            except ProviderMediaPublicationError:
                # Relay TTL cleanup is the fallback after an invalid response.
                pass
            raise

    def release(self, cleanup_token: str) -> None:
        object_id = self._cleanup_token(cleanup_token)
        self._request("DELETE", f"/v1/objects/{object_id}", expected_status=204)

    def purge_expired(self) -> int:
        payload = self._request("POST", "/v1/cleanup", expected_status=200)
        deleted = payload.get("deleted")
        if type(deleted) is not int or deleted < 0:
            raise ProviderMediaPublicationError("HTTPS 素材中转清理响应无效")
        return deleted
