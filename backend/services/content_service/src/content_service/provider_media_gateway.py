from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

_SIGNATURE_VERSION = "v1"
_MIN_SECRET_BYTES = 32


def normalize_provider_media_key(key: str) -> str:
    """Return the one canonical object-key representation used for signing."""

    if (
        not key
        or "\\" in key
        or any(ord(character) < 32 or ord(character) == 127 for character in key)
    ):
        raise ValueError("Provider 媒体 key 必须是非空 POSIX 相对路径")
    candidate = PurePosixPath(key)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Provider 媒体 key 超出素材目录")
    normalized = candidate.as_posix()
    if normalized in {"", "."} or normalized != key:
        raise ValueError("Provider 媒体 key 不是规范的 POSIX 相对路径")
    return normalized


class ProviderMediaUrlSigner:
    """Issue and verify short-lived bearer URLs for provider media fetches.

    The signature binds the expiry and the canonical object key.  A signer is
    deliberately disabled when neither a public HTTPS base nor a secret is
    configured.  Supplying only one side is an operator error and fails fast.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        signing_secret: str | None = None,
        ttl_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        normalized_base = str(base_url or "").strip()
        normalized_secret = str(signing_secret or "").strip()
        if bool(normalized_base) != bool(normalized_secret):
            raise ValueError("Provider 媒体网关必须同时配置 HTTPS 基址和 HMAC 签名密钥")
        if not 60 <= int(ttl_seconds) <= 3600:
            raise ValueError("Provider 媒体签名 URL 有效期必须在 60 到 3600 秒之间")

        self.base_url: str | None = None
        self._secret: bytes | None = None
        self.ttl_seconds = int(ttl_seconds)
        self._clock = clock
        if not normalized_base:
            return

        parsed = urlsplit(normalized_base)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Provider 媒体网关基址必须是无凭据、无查询串、无片段的 HTTPS URL")
        secret_bytes = normalized_secret.encode("utf-8")
        if len(secret_bytes) < _MIN_SECRET_BYTES:
            raise ValueError("Provider 媒体 HMAC 签名密钥至少需要 32 字节")
        self.base_url = urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self._secret = secret_bytes

    @property
    def enabled(self) -> bool:
        return self.base_url is not None and self._secret is not None

    @staticmethod
    def _payload(key: str, expires: int) -> bytes:
        return f"{_SIGNATURE_VERSION}\n{expires}\n{key}".encode()

    def _signature(self, key: str, expires: int) -> str:
        if self._secret is None:
            raise RuntimeError("Provider 媒体网关未配置")
        return hmac.new(
            self._secret,
            self._payload(key, expires),
            hashlib.sha256,
        ).hexdigest()

    def create_url(self, key: str) -> str | None:
        if not self.enabled:
            return None
        normalized_key = normalize_provider_media_key(key)
        expires = int(self._clock()) + self.ttl_seconds
        signature = self._signature(normalized_key, expires)
        parsed = urlsplit(self.base_url or "")
        path = f"{parsed.path.rstrip('/')}/{quote(normalized_key, safe='/')}"
        query = urlencode({"expires": expires, "signature": signature})
        return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))

    def verify(self, key: str, *, expires: int, signature: str) -> bool:
        if not self.enabled:
            return False
        try:
            normalized_key = normalize_provider_media_key(key)
            normalized_expires = int(expires)
        except (TypeError, ValueError):
            return False
        if normalized_expires <= int(self._clock()):
            return False
        supplied = str(signature or "").strip().lower()
        if len(supplied) != 64 or any(char not in "0123456789abcdef" for char in supplied):
            return False
        expected = self._signature(normalized_key, normalized_expires)
        return hmac.compare_digest(expected, supplied)
