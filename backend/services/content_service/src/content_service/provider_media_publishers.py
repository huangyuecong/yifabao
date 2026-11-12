from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .media_ingest import (
    MediaIngestPolicy,
    MediaValidationError,
    read_upload_head,
    validate_upload_header,
)
from .providers import ProviderMediaPublication

_REFERENCE_VIDEO_MEDIA_TYPES = frozenset({"video/mp4", "video/quicktime"})
_REFERENCE_VIDEO_EXTENSIONS = {"video/mp4": ".mp4", "video/quicktime": ".mov"}
_MAX_REFERENCE_VIDEO_BYTES = 200 * 1024 * 1024


class ProviderMediaPublicationError(RuntimeError):
    """Fail-closed error at the temporary provider-media boundary."""


class DisabledProviderMediaPublisher:
    enabled = False
    transport = "unavailable"
    url_ttl_seconds = None

    def publish(
        self,
        source_path: Path,
        *,
        media_type: str,
        publication_id: str,
    ) -> ProviderMediaPublication:
        del source_path, media_type, publication_id
        raise ProviderMediaPublicationError("未配置私有对象存储临时发布器")

    def release(self, cleanup_token: str) -> None:
        del cleanup_token

    def purge_expired(self) -> int:
        return 0


class TosProviderMediaPublisher:
    """Publish local reference videos to private Volcengine TOS objects.

    Object keys are confined to one operator-configured prefix and include the
    signed-URL expiry epoch.  The TOS object remains private; only a short-lived
    pre-signed GET URL is returned.  No signed URL is logged or persisted here.
    """

    enabled = True
    transport = "tos-private-presigned-https"

    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        security_token: str | None,
        key_prefix: str,
        url_ttl_seconds: int,
        object_ttl_days: int,
        allowed_root: Path,
        connect_timeout_seconds: float = 10,
        socket_timeout_seconds: float = 60,
        client: Any | None = None,
        sdk_module: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.region = region
        self.bucket = bucket
        self.key_prefix = self._normalize_prefix(key_prefix)
        self.url_ttl_seconds = int(url_ttl_seconds)
        self.object_ttl_days = int(object_ttl_days)
        self.allowed_root = allowed_root.expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        if not 60 <= self.url_ttl_seconds <= 3600:
            raise ValueError("TOS 预签名 URL 有效期必须在 60 到 3600 秒之间")
        if not 1 <= self.object_ttl_days <= 7:
            raise ValueError("TOS 临时对象兜底生命周期必须在 1 到 7 天之间")
        if client is None:
            if sdk_module is None:
                try:
                    import tos as sdk_module  # type: ignore[import-not-found,no-redef]
                except ImportError as exc:
                    raise RuntimeError(
                        "已配置 TOS 临时发布器，但未安装官方 tos Python SDK"
                    ) from exc
            client = sdk_module.TosClientV2(
                access_key,
                secret_key,
                endpoint,
                region,
                security_token=security_token,
                connection_time=connect_timeout_seconds,
                socket_timeout=socket_timeout_seconds,
                enable_verify_ssl=True,
            )
        self._client = client
        self._private_acl = sdk_module.ACLType.ACL_Private if sdk_module is not None else "private"
        self._http_get = (
            sdk_module.HttpMethodType.Http_Method_Get if sdk_module is not None else "GET"
        )

    @staticmethod
    def _normalize_prefix(value: str) -> str:
        prefix = value.strip().strip("/")
        if not prefix or "\\" in prefix:
            raise ValueError("TOS 临时对象前缀必须是非空 POSIX 相对路径")
        parts = prefix.split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise ValueError("TOS 临时对象前缀包含无效路径段")
        return "/".join(parts)

    def _now(self) -> datetime:
        value = self._clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _validated_source(self, source_path: Path, media_type: str) -> tuple[Path, int, str]:
        path = source_path.expanduser().resolve()
        try:
            path.relative_to(self.allowed_root)
        except ValueError as exc:
            raise ProviderMediaPublicationError("临时发布源文件不在内容素材目录内") from exc
        if media_type not in _REFERENCE_VIDEO_MEDIA_TYPES:
            raise ProviderMediaPublicationError("临时发布仅允许 MP4 或 MOV 参考视频")
        expected_extension = _REFERENCE_VIDEO_EXTENSIONS[media_type]
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            raise ProviderMediaPublicationError("参考视频文件不可用") from exc
        if size_bytes < 1 or size_bytes > _MAX_REFERENCE_VIDEO_BYTES:
            raise ProviderMediaPublicationError("参考视频大小必须在 1 字节到 200MB 之间")
        try:
            with path.open("rb") as source:
                head = read_upload_head(source)
            media_kind, canonical_media_type = validate_upload_header(
                filename=f"reference{expected_extension}",
                declared_media_type=media_type,
                head=head,
                size_bytes=size_bytes,
                policy=MediaIngestPolicy(),
            )
        except (MediaValidationError, OSError) as exc:
            raise ProviderMediaPublicationError("参考视频文件格式校验失败") from exc
        if media_kind != "video" or canonical_media_type != media_type:
            raise ProviderMediaPublicationError("临时发布仅允许 MP4 或 MOV 参考视频")
        return path, size_bytes, expected_extension

    def _validate_cleanup_token(self, cleanup_token: str) -> str:
        prefix = f"{self.key_prefix}/"
        if (
            not cleanup_token.startswith(prefix)
            or "\\" in cleanup_token
            or cleanup_token.startswith("/")
        ):
            raise ProviderMediaPublicationError("TOS 临时对象清理标识无效")
        relative = cleanup_token[len(prefix) :]
        if not re.fullmatch(
            r"\d{10,12}/[0-9a-f]{24}/[0-9a-f]{32}\.(?:mp4|mov)",
            relative,
        ):
            raise ProviderMediaPublicationError("TOS 临时对象清理标识无效")
        return cleanup_token

    @staticmethod
    def _validated_signed_url(value: object) -> str:
        url = str(value or "")
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.query
        ):
            raise ProviderMediaPublicationError("TOS 未返回有效的短时预签名 HTTPS URL")
        return url

    def publish(
        self,
        source_path: Path,
        *,
        media_type: str,
        publication_id: str,
    ) -> ProviderMediaPublication:
        path, size_bytes, extension = self._validated_source(source_path, media_type)
        now = self._now()
        planned_cleanup_at = now + timedelta(seconds=self.url_ttl_seconds)
        safe_publication_id = hashlib.sha256(publication_id.encode("utf-8")).hexdigest()[:24]
        object_key = (
            f"{self.key_prefix}/{int(planned_cleanup_at.timestamp())}/"
            f"{safe_publication_id}/{uuid4().hex}{extension}"
        )
        uploaded = False
        try:
            with path.open("rb") as source:
                self._client.put_object(
                    self.bucket,
                    object_key,
                    content=source,
                    content_length=size_bytes,
                    content_type=media_type,
                    acl=self._private_acl,
                    forbid_overwrite=True,
                    object_expires=self.object_ttl_days,
                    meta={
                        "purpose": "seedance-reference-video",
                        "url-ttl-seconds": str(self.url_ttl_seconds),
                    },
                )
            uploaded = True
            # A pre-signed URL starts its TTL when it is generated, not when a
            # potentially large (up to 200MB) upload began.
            expires_at = self._now() + timedelta(seconds=self.url_ttl_seconds)
            signed = self._client.pre_signed_url(
                self._http_get,
                self.bucket,
                object_key,
                expires=self.url_ttl_seconds,
            )
            url = self._validated_signed_url(getattr(signed, "signed_url", None))
        except ProviderMediaPublicationError:
            if uploaded:
                self._delete_after_failed_publish(object_key)
            raise
        except Exception as exc:
            if uploaded:
                self._delete_after_failed_publish(object_key)
            raise ProviderMediaPublicationError("TOS 参考视频临时发布失败") from exc
        return ProviderMediaPublication(
            url=url,
            transport=self.transport,
            expires_at=expires_at,
            cleanup_token=object_key,
        )

    def _delete_after_failed_publish(self, object_key: str) -> None:
        try:  # noqa: SIM105
            self._client.delete_object(self.bucket, object_key)
        except Exception:
            # object_expires plus the bucket lifecycle rule are the crash/failure backstop
            pass

    def release(self, cleanup_token: str) -> None:
        object_key = self._validate_cleanup_token(cleanup_token)
        try:
            self._client.delete_object(self.bucket, object_key)
        except Exception as exc:
            raise ProviderMediaPublicationError("TOS 临时参考视频清理失败") from exc

    def purge_expired(self) -> int:
        try:
            listed = self._client.list_objects_type2(
                self.bucket,
                prefix=f"{self.key_prefix}/",
                max_keys=1000,
                list_only_once=True,
            )
        except Exception as exc:
            raise ProviderMediaPublicationError("TOS 临时参考视频过期扫描失败") from exc
        now_epoch = int(self._now().timestamp())
        expired: list[str] = []
        for item in getattr(listed, "contents", ()):
            key = str(getattr(item, "key", "") or "")
            try:
                validated = self._validate_cleanup_token(key)
                expiry_epoch = int(validated[len(self.key_prefix) + 1 :].split("/", 1)[0])
            except (ProviderMediaPublicationError, ValueError):
                continue
            last_modified = getattr(item, "last_modified", None)
            if isinstance(last_modified, datetime):
                if last_modified.tzinfo is None:
                    last_modified = last_modified.replace(tzinfo=UTC)
                expiry_epoch = max(
                    expiry_epoch,
                    int(last_modified.timestamp()) + self.url_ttl_seconds,
                )
            if expiry_epoch <= now_epoch:
                expired.append(validated)
        deleted = 0
        for object_key in expired:
            try:
                self._client.delete_object(self.bucket, object_key)
            except Exception:
                continue
            deleted += 1
        return deleted
