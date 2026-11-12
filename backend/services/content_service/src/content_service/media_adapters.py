from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import io
import ipaddress
import json
import math
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw

from .generation_errors import safe_provider_error_metadata
from .hiagent_network import hiagent_download_httpx_options, safe_result_download_url
from .llm_adapters import ProviderCallError
from .provider_media_gateway import ProviderMediaUrlSigner
from .provider_native_video import PROVIDER_NATIVE_VIDEO_TRANSPORT
from .providers import (
    AiVideoGenerationPort,
    AiVideoGenerationRequest,
    AiVideoTask,
    AssetStorePort,
    AssetWriteResult,
    GeneratedAsset,
    ImageGenerationRequest,
    MusicGenerationRequest,
    MusicGenerationResult,
    ProviderStatus,
    SpeechSynthesisRequest,
    SpeechSynthesisResult,
    SubtitleAlignmentRequest,
    SubtitleSegment,
    VideoClip,
    VideoCompositionRequest,
    VideoCompositionResult,
    WordTiming,
)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_TERMINAL_VIDEO_STATUSES = {"cancelled", "succeeded", "failed", "expired"}
_ARK_VIDEO_STATUSES = {
    "queued",
    "running",
    "cancelled",
    "succeeded",
    "failed",
    "expired",
}
_MAX_IMAGE_GENERATION_REFERENCES = 9
_MAX_AI_VIDEO_REFERENCE_IMAGES = 9
_MAX_AI_VIDEO_REFERENCE_VIDEOS = 3
_MIN_AI_VIDEO_REFERENCE_VIDEO_SECONDS = 2.0
_MAX_AI_VIDEO_REFERENCE_VIDEO_SECONDS = 15.0
_MAX_AI_VIDEO_REFERENCE_VIDEO_TOTAL_SECONDS = 15.0
_MAX_ARK_AI_VIDEO_REQUEST_BODY_BYTES = 64 * 1024 * 1024
_ARK_INPUT_IMAGE_PRIVACY_CODE = "InputImageSensitiveContentDetected.PrivacyInformation"
_MEDIA_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "pcm": "audio/L16",
    "ogg_opus": "audio/ogg",
}


def provider_capability_document(port: Any) -> dict[str, Any]:
    status = port.status
    return {
        "provider_id": status.provider_id,
        "name": status.name,
        "capability": status.capability,
        "mode": status.mode,
        "external": status.external,
        "enabled": status.enabled,
        "model": status.model,
        "parameters": dict(getattr(port, "capabilities", {})),
    }


def _provider_endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    suffix = "/" + suffix.lstrip("/")
    if base.endswith(suffix):
        return base
    return base + suffix


def _safe_http_error(provider_id: str, exc: Exception) -> ProviderCallError:
    if isinstance(exc, httpx.HTTPStatusError):
        details = safe_provider_error_metadata(exc.response)
        provider_error_code = details.get("provider_code", "")
        reason = " / ".join(
            details[key] for key in ("provider_code", "message") if details.get(key)
        )
        detail = f"：{reason}" if reason else ""
        is_input_image_privacy_error = provider_error_code == _ARK_INPUT_IMAGE_PRIVACY_CODE
        retryable = exc.response.status_code in {408, 425, 429} or (exc.response.status_code >= 500)
        return ProviderCallError(
            f"{provider_id} 返回 HTTP {exc.response.status_code}{detail}",
            code=(
                "input_image_privacy_information"
                if is_input_image_privacy_error
                else "provider_call_failed"
            ),
            retryable=False if is_input_image_privacy_error else retryable,
            status_code=422 if is_input_image_privacy_error else exc.response.status_code,
            provider_code=details.get("provider_code"),
            provider_request_id=details.get("provider_request_id"),
            provider_status_code=exc.response.status_code,
        )
    return ProviderCallError(
        f"{provider_id} 请求失败：{type(exc).__name__}",
        retryable=isinstance(exc, httpx.TransportError),
    )


def _decode_base64(value: str, provider_id: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProviderCallError(f"{provider_id} 返回了无效的 Base64 媒体数据") from exc
    if not decoded:
        raise ProviderCallError(f"{provider_id} 返回了空媒体数据")
    return decoded


def _validate_audio_content(
    content: bytes,
    audio_format: str,
    provider_id: str,
    *,
    max_bytes: int,
) -> None:
    if not content:
        raise ProviderCallError(f"{provider_id} 返回了空音频")
    if len(content) > max_bytes:
        raise ProviderCallError(f"{provider_id} 返回的音频超过允许大小")
    if audio_format == "wav":
        valid = len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE"
    else:
        valid = content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
        )
    if not valid:
        raise ProviderCallError(f"{provider_id} 返回内容不是有效的 {audio_format.upper()} 音频")


def _bounded_response_bytes(response: Any, *, max_bytes: int, provider_id: str) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise ProviderCallError(f"{provider_id} 返回无效 Content-Length") from exc
        if declared_size > max_bytes:
            raise ProviderCallError(f"{provider_id} 返回内容超过允许大小")
    total = 0
    output = bytearray()
    iterator = getattr(response, "iter_bytes", None)
    chunks = iterator(1024 * 1024) if callable(iterator) else (bytes(response.content),)
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ProviderCallError(f"{provider_id} 返回内容超过允许大小")
        output.extend(chunk)
    if not output:
        raise ProviderCallError(f"{provider_id} 返回内容为空")
    return bytes(output)


class LocalAssetStore(AssetStorePort):
    """Content-service-owned filesystem storage with path traversal protection."""

    def __init__(
        self,
        root: Path,
        *,
        provider_media_url_signer: ProviderMediaUrlSigner | None = None,
    ):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider_media_url_signer = provider_media_url_signer or ProviderMediaUrlSigner()

    def path_for(self, key: str) -> Path:
        relative = Path(key)
        if not key or relative.is_absolute() or bool(relative.drive):
            raise ValueError("素材存储 key 必须是非空相对路径")
        if ".." in relative.parts:
            raise ValueError("素材存储 key 超出内容服务目录")

        # On Windows, resolving a path while another upload is creating the
        # same parent directory can transiently return an incomplete path.  It
        # made valid parallel uploads look like traversal attempts.  Create the
        # generated parent first, then resolve that existing directory for the
        # containment check.  This also preserves the symlink-escape guard.
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve(strict=True)
        if resolved_parent != self.root and self.root not in resolved_parent.parents:
            raise ValueError("素材存储 key 超出内容服务目录")
        resolved_target = resolved_parent / target.name
        if resolved_target.exists() or resolved_target.is_symlink():
            resolved_target = resolved_target.resolve(strict=True)
            if resolved_target != self.root and self.root not in resolved_target.parents:
                raise ValueError("素材存储 key 超出内容服务目录")
        return resolved_target

    @staticmethod
    def _result(key: str, path: Path, media_type: str) -> AssetWriteResult:
        content = path.read_bytes()
        return AssetWriteResult(
            key=key,
            size=len(content),
            media_type=media_type,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        )

    def put_bytes(self, key: str, content: bytes, media_type: str) -> AssetWriteResult:
        if not isinstance(content, bytes) or not content:
            raise ValueError("素材内容不能为空")
        path = self.path_for(key)
        path.write_bytes(content)
        return self._result(key, path, media_type)

    def put_stream(self, key: str, stream, media_type: str) -> AssetWriteResult:
        path = self.path_for(key)
        with path.open("wb") as output:
            while chunk := stream.read(1024 * 1024):
                output.write(chunk)
        if path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            raise ValueError("素材内容不能为空")
        return self._result(key, path, media_type)

    def read_bytes(self, key: str) -> bytes:
        return self.path_for(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def provider_url(self, key: str) -> str | None:
        """Map an existing object key to a short-lived provider-fetchable URL.

        The external endpoint is a bearer URL protected by HMAC and expiry; the
        object directory itself is never exposed anonymously.
        """

        if not self.exists(key):
            return None
        return self.provider_media_url_signer.create_url(Path(key).as_posix())


class MockImageGenerationPort:
    capabilities: ClassVar[dict[str, Any]] = {
        "count": {"min": 1, "max": 8},
        "size": {"mode": "custom", "min": 64, "max": 4096},
        "reference_images": {
            "max": _MAX_IMAGE_GENERATION_REFERENCES,
            "formats": ["image/jpeg", "image/png", "image/webp"],
            "max_bytes_per_file": 30 * 1024 * 1024,
        },
        "watermark": False,
    }

    def __init__(
        self,
        *,
        provider_id: str = "mock-image",
        model: str = "deterministic-image",
        max_candidates: int = 8,
    ):
        if max_candidates < 1:
            raise ValueError("图片最大候选数必须大于 0")
        self.max_candidates = max_candidates
        self.capabilities = {
            **type(self).capabilities,
            "count": {"min": 1, "max": max_candidates},
        }
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="deterministic-image",
            capability="image-generation",
            mode="mock",
            external=False,
            enabled=True,
            model=model,
        )

    def generate(self, request: ImageGenerationRequest) -> Sequence[GeneratedAsset]:
        if not request.prompt.strip():
            raise ValueError("图片提示词不能为空")
        if not 1 <= request.count <= self.max_candidates:
            raise ValueError(f"单次图片候选数量必须在 1 到 {self.max_candidates} 之间")
        if len(request.reference_images) > _MAX_IMAGE_GENERATION_REFERENCES:
            raise ValueError(f"单次图片生成最多允许 {_MAX_IMAGE_GENERATION_REFERENCES} 张参考图片")
        if request.reference_media_types and len(request.reference_images) != len(
            request.reference_media_types
        ):
            raise ValueError("参考图片与媒体类型数量不一致")
        if any(len(content) > 30 * 1024 * 1024 for content in request.reference_images):
            raise ValueError("单张参考图片不能超过 30MB")
        if any(
            media_type.split(";", 1)[0].strip().lower()
            not in {"image/jpeg", "image/png", "image/webp"}
            for media_type in request.reference_media_types
        ):
            raise ValueError("参考图片必须是 JPEG、PNG 或 WebP")
        width, height = _parse_image_size(request.size or "1280x720")
        values: list[GeneratedAsset] = []
        for index in range(request.count):
            image = Image.new("RGB", (width, height), "#f5f7fa")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, width, max(64, height // 9)), fill="#197465")
            margin = max(24, min(width, height) // 16)
            draw.rectangle(
                (margin, height // 4, width - margin, height - margin),
                fill="#e8f5f2",
                outline="#cddbd8",
                width=3,
            )
            draw.text((margin, margin), "CONTENT MOCK", fill="white")
            draw.text((margin * 2, height // 3), request.prompt[:48], fill="#222222")
            draw.text((margin * 2, height // 3 + 36), f"VARIANT {index + 1}", fill="#666666")
            output = io.BytesIO()
            image.save(output, "PNG")
            values.append(
                GeneratedAsset(
                    content=output.getvalue(),
                    media_type="image/png",
                    extension="png",
                    provider_id=self.status.provider_id,
                    model=self.status.model or "",
                    metadata={
                        "width": width,
                        "height": height,
                        "ratio": request.ratio or f"{width}:{height}",
                        "mock": True,
                        "candidate_index": index,
                    },
                )
            )
        return values


def _parse_image_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2,4})[xX×](\d{2,4})", value.strip())
    if not match:
        raise ValueError("图片尺寸格式应为 宽x高，例如 1080x1920")
    width, height = int(match.group(1)), int(match.group(2))
    if not 64 <= width <= 4096 or not 64 <= height <= 4096:
        raise ValueError("图片宽高必须在 64 到 4096 像素之间")
    return width, height


class VolcengineArkImageGenerationPort:
    _MODERN_SEEDREAM_SIZES: ClassVar[tuple[str, ...]] = ("2K", "3K", "4K")
    _MODERN_SEEDREAM_MIN_PIXELS: ClassVar[int] = 3_686_400
    _MODERN_SEEDREAM_MAX_PIXELS: ClassVar[int] = 16_777_216
    _MODERN_SEEDREAM_MIN_ASPECT_RATIO: ClassVar[float] = 1 / 16
    _MODERN_SEEDREAM_MAX_ASPECT_RATIO: ClassVar[float] = 16
    _MODERN_SEEDREAM_2K_BY_RATIO: ClassVar[dict[str, str]] = {
        "1:1": "2048x2048",
        "4:3": "2304x1728",
        "3:4": "1728x2304",
        "3:2": "2496x1664",
        "2:3": "1664x2496",
        "16:9": "2560x1440",
        "9:16": "1440x2560",
        "21:9": "3024x1296",
    }
    _MODERN_SEEDREAM_RATIOS: ClassVar[tuple[str, ...]] = (
        "adaptive",
        "1:1",
        "4:3",
        "3:4",
        "3:2",
        "2:3",
        "16:9",
        "9:16",
        "21:9",
    )

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_candidates: int = 4,
        supported_sizes: tuple[str, ...] = (),
        supported_ratios: tuple[str, ...] = (),
    ):
        self.endpoint = _provider_endpoint(base_url, "/images/generations")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        if max_candidates < 1:
            raise ValueError("图片最大候选数必须大于 0")
        self.max_candidates = max_candidates
        modern_seedream = any(marker in model.lower() for marker in ("seedream-4", "seedream-5"))
        self._modern_seedream = modern_seedream
        effective_sizes = supported_sizes or (
            self._MODERN_SEEDREAM_SIZES if modern_seedream else ()
        )
        effective_ratios = supported_ratios or (
            self._MODERN_SEEDREAM_RATIOS if modern_seedream else ()
        )
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="volcengine-ark-image",
            capability="image-generation",
            mode="api",
            external=True,
            enabled=True,
            model=model,
        )
        self.capabilities = {
            "count": {"min": 1, "max": max_candidates},
            "sizes": list(effective_sizes),
            "ratios": list(effective_ratios),
            "custom_size": modern_seedream,
            "custom_ratio": modern_seedream,
            "minimum_pixels": (self._MODERN_SEEDREAM_MIN_PIXELS if modern_seedream else None),
            "maximum_pixels": (self._MODERN_SEEDREAM_MAX_PIXELS if modern_seedream else None),
            "aspect_ratio_range": (
                {
                    "min": self._MODERN_SEEDREAM_MIN_ASPECT_RATIO,
                    "max": self._MODERN_SEEDREAM_MAX_ASPECT_RATIO,
                }
                if modern_seedream
                else None
            ),
            "defaults": {
                "size": "2K" if modern_seedream else None,
                "ratio": "adaptive" if modern_seedream else None,
            },
            "parameter_source": (
                "service-configuration"
                if supported_sizes or supported_ratios
                else "provider-contract"
            ),
            "reference_images": {
                "max": _MAX_IMAGE_GENERATION_REFERENCES,
                "formats": ["image/jpeg", "image/png", "image/webp"],
                "max_bytes_per_file": 30 * 1024 * 1024,
            },
            "watermark": True,
        }

    def _effective_size(self, size: str | None, ratio: str | None) -> str | None:
        if not self._modern_seedream or not size:
            return size
        normalized = size.strip().replace("X", "x").replace("×", "x")
        resolution = normalized.upper()
        if resolution in self._MODERN_SEEDREAM_SIZES:
            return resolution
        match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", normalized)
        if not match:
            raise ValueError("Seedream 图片尺寸应为 2K、3K、4K 或 宽x高")
        width, height = int(match.group(1)), int(match.group(2))
        pixels = width * height
        aspect_ratio = width / height
        if not (
            self._MODERN_SEEDREAM_MIN_ASPECT_RATIO
            <= aspect_ratio
            <= self._MODERN_SEEDREAM_MAX_ASPECT_RATIO
        ):
            raise ValueError("Seedream 图片宽高比必须在 1:16 到 16:1 之间")
        if pixels > self._MODERN_SEEDREAM_MAX_PIXELS:
            raise ValueError("Seedream 图片总像素不能超过 1677.72 万")
        if pixels >= self._MODERN_SEEDREAM_MIN_PIXELS:
            return normalized
        selected_ratio = ratio if ratio in self._MODERN_SEEDREAM_2K_BY_RATIO else None
        if selected_ratio is None:
            for candidate, candidate_size in self._MODERN_SEEDREAM_2K_BY_RATIO.items():
                target_width, target_height = (
                    int(value) for value in candidate_size.split("x", maxsplit=1)
                )
                if width * target_height == height * target_width:
                    selected_ratio = candidate
                    break
        return self._MODERN_SEEDREAM_2K_BY_RATIO.get(selected_ratio or "", "2K")

    def generate(self, request: ImageGenerationRequest) -> Sequence[GeneratedAsset]:
        if not request.prompt.strip():
            raise ValueError("图片提示词不能为空")
        if not 1 <= request.count <= self.max_candidates:
            raise ValueError(f"单次图片候选数量必须在 1 到 {self.max_candidates} 之间")
        if len(request.reference_images) > _MAX_IMAGE_GENERATION_REFERENCES:
            raise ValueError(f"单次图片生成最多允许 {_MAX_IMAGE_GENERATION_REFERENCES} 张参考图片")
        if request.reference_media_types and len(request.reference_images) != len(
            request.reference_media_types
        ):
            raise ValueError("参考图片与媒体类型数量不一致")
        if any(len(content) > 30 * 1024 * 1024 for content in request.reference_images):
            raise ValueError("单张参考图片不能超过 30MB")
        if any(
            media_type.split(";", 1)[0].strip().lower()
            not in {"image/jpeg", "image/png", "image/webp"}
            for media_type in request.reference_media_types
        ):
            raise ValueError("参考图片必须是 JPEG、PNG 或 WebP")
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "response_format": "b64_json",
            "watermark": request.watermark,
        }
        effective_size = self._effective_size(request.size, request.ratio)
        if effective_size:
            payload["size"] = effective_size
        if self._modern_seedream:
            payload["sequential_image_generation"] = "disabled"
        if request.reference_images:
            media_types = request.reference_media_types
            payload["image"] = [
                "data:"
                + (media_types[index] if index < len(media_types) else "image/png")
                + ";base64,"
                + base64.b64encode(content).decode("ascii")
                for index, content in enumerate(request.reference_images)
            ]
        payload.update(request.extra)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            values: list[GeneratedAsset] = []
            with httpx.Client(timeout=self.timeout_seconds) as client:
                # Ark's current ImageGenerations contract does not expose a
                # provider-neutral `n` field. Request candidates separately so
                # multi-candidate behavior remains valid across Seedream models.
                for index in range(request.count):
                    candidate_payload = dict(payload)
                    if request.seed is not None:
                        candidate_payload["seed"] = request.seed + index
                    response = client.post(
                        self.endpoint,
                        headers=headers,
                        json=candidate_payload,
                    )
                    response.raise_for_status()
                    response_payload = response.json()
                    if not isinstance(response_payload, dict):
                        raise TypeError("顶层不是 JSON 对象")
                    raw_items = response_payload.get("data")
                    if not isinstance(raw_items, list) or not raw_items:
                        raise ValueError("缺少 data 图片列表")
                    item = raw_items[0]
                    if not isinstance(item, dict):
                        raise TypeError("图片结果结构无效")
                    if isinstance(item.get("b64_json"), str):
                        content = _decode_base64(item["b64_json"], self.status.provider_id)
                        media_type = "image/png"
                    elif isinstance(item.get("url"), str):
                        download = client.get(item["url"])
                        download.raise_for_status()
                        content = bytes(download.content)
                        if not content:
                            raise ValueError("图片下载结果为空")
                        headers_value = getattr(download, "headers", {})
                        media_type = str(headers_value.get("content-type", "image/png")).split(";")[
                            0
                        ]
                    else:
                        raise TypeError("图片结果缺少 b64_json 或 url")
                    extension = {
                        "image/jpeg": "jpg",
                        "image/webp": "webp",
                    }.get(media_type, "png")
                    values.append(
                        GeneratedAsset(
                            content=content,
                            media_type=media_type,
                            extension=extension,
                            provider_id=self.status.provider_id,
                            model=self.model,
                            metadata={
                                "candidate_index": index,
                                "size": item.get("size") or effective_size,
                                "requested_size": request.size,
                                "usage": response_payload.get("usage", {}),
                            },
                        )
                    )
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise ProviderCallError(f"{self.status.provider_id} 返回的图片结果无效：{exc}") from exc
        if len(values) != request.count:
            raise ProviderCallError(
                f"{self.status.provider_id} 返回 {len(values)} 张图片，预期 {request.count} 张"
            )
        return values


_MUSIC_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}
_MINIMAX_MUSIC_SAMPLE_RATES = {16000, 24000, 32000, 44100}
_MINIMAX_MUSIC_BITRATES = {32000, 64000, 128000, 256000}
_MUSIC_RESPONSE_FORMATS = {"hex", "url"}
_DEFAULT_MINIMAX_DOWNLOAD_HOSTS = ("minimax.io", "minimaxi.com", "minimax.chat")
_MUSIC_CONTENT_TYPES = {
    "mp3": {"audio/mpeg", "audio/mp3", "application/octet-stream"},
    "wav": {
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "application/octet-stream",
    },
}


def _validate_music_request(
    request: MusicGenerationRequest,
    *,
    minimum_duration_seconds: float,
    maximum_duration_seconds: float,
) -> str:
    prompt = request.prompt.strip()
    if not prompt:
        raise ValueError("配乐提示词不能为空")
    if len(prompt) > 2000:
        raise ValueError("配乐提示词不能超过 2000 个字符")
    if not minimum_duration_seconds <= request.duration_seconds <= maximum_duration_seconds:
        raise ValueError(
            "配乐目标时长必须在 "
            f"{minimum_duration_seconds:g} 到 {maximum_duration_seconds:g} 秒之间"
        )
    if request.audio_format not in _MUSIC_FORMATS:
        raise ValueError("配乐格式仅支持 mp3 或 wav")
    if request.sample_rate not in _MINIMAX_MUSIC_SAMPLE_RATES:
        raise ValueError("配乐采样率仅支持 16000、24000、32000 或 44100")
    if request.bitrate not in _MINIMAX_MUSIC_BITRATES:
        raise ValueError("配乐码率仅支持 32000、64000、128000 或 256000")
    if (
        request.response_format is not None
        and request.response_format not in _MUSIC_RESPONSE_FORMATS
    ):
        raise ValueError("配乐供应商返回格式仅支持 hex 或 url")
    return prompt


def _mock_music_wav(duration_seconds: float, sample_rate: int) -> bytes:
    """Create a quiet deterministic stereo cue without an external service."""

    frames = max(1, round(duration_seconds * sample_rate))
    one_second = bytearray()
    for frame in range(sample_rate):
        # A deliberately quiet two-note pad that is recognizable as mock data.
        value = int(
            700 * math.sin(2 * math.pi * 220 * frame / sample_rate)
            + 350 * math.sin(2 * math.pi * 329.63 * frame / sample_rate)
        )
        packed = struct.pack("<h", value)
        one_second.extend(packed)
        one_second.extend(packed)
    whole_seconds, remaining = divmod(frames, sample_rate)
    pcm = bytes(one_second) * whole_seconds + bytes(one_second[: remaining * 4])
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def _mock_wav_to_mp3(content: bytes, *, max_bytes: int) -> bytes:
    with tempfile.TemporaryDirectory(prefix="content-music-mock-") as directory:
        root = Path(directory)
        source = root / "music.wav"
        output = root / "music.mp3"
        source.write_bytes(content)
        try:
            subprocess.run(
                [
                    _ffmpeg_executable(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    str(output),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError("本地 Mock 配乐无法编码 MP3") from exc
        encoded = output.read_bytes()
    _validate_audio_content(encoded, "mp3", "mock-music", max_bytes=max_bytes)
    return encoded


class MockMusicGenerationPort:
    def __init__(
        self,
        *,
        provider_id: str = "mock-music",
        model: str = "deterministic-instrumental",
        minimum_duration_seconds: float = 1,
        maximum_duration_seconds: float = 300,
        max_bytes: int = 64 * 1024 * 1024,
    ):
        if minimum_duration_seconds <= 0 or maximum_duration_seconds < minimum_duration_seconds:
            raise ValueError("Mock 配乐时长范围无效")
        if max_bytes < 1024:
            raise ValueError("配乐最大字节数过小")
        self.minimum_duration_seconds = minimum_duration_seconds
        self.maximum_duration_seconds = maximum_duration_seconds
        self.max_bytes = max_bytes
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="deterministic-instrumental-music",
            capability="music-generation",
            mode="mock",
            external=False,
            enabled=True,
            model=model,
        )
        self.capabilities = {
            "instrumental_only": True,
            "formats": sorted(_MUSIC_FORMATS),
            "response_formats": sorted(_MUSIC_RESPONSE_FORMATS),
            "duration_seconds": {
                "minimum": minimum_duration_seconds,
                "maximum": maximum_duration_seconds,
                "control": "exact",
            },
            "returns_bytes": True,
            "mock": True,
        }

    def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        _validate_music_request(
            request,
            minimum_duration_seconds=self.minimum_duration_seconds,
            maximum_duration_seconds=self.maximum_duration_seconds,
        )
        wav = _mock_music_wav(request.duration_seconds, request.sample_rate)
        audio = (
            wav
            if request.audio_format == "wav"
            else _mock_wav_to_mp3(
                wav,
                max_bytes=self.max_bytes,
            )
        )
        _validate_audio_content(
            audio,
            request.audio_format,
            self.status.provider_id,
            max_bytes=self.max_bytes,
        )
        return MusicGenerationResult(
            audio=audio,
            media_type=_MUSIC_FORMATS[request.audio_format],
            extension=request.audio_format,
            duration_seconds=request.duration_seconds,
            requested_duration_seconds=request.duration_seconds,
            provider_id=self.status.provider_id,
            model=self.status.model or "",
            metadata={
                "mock": True,
                "instrumental": True,
                "sample_rate": request.sample_rate,
                "bitrate": request.bitrate,
                "supplier_response_format": request.response_format or "hex",
                "duration_control": "exact",
            },
        )


def _safe_supplier_audio_url(
    value: str,
    provider_id: str,
    allowed_download_hosts: tuple[str, ...],
) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProviderCallError(f"{provider_id} 返回的音频地址必须是 HTTPS")
    if parsed.username or parsed.password:
        raise ProviderCallError(f"{provider_id} 返回的音频地址不能包含用户凭据")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise ProviderCallError(f"{provider_id} 返回的音频地址不能指向非公网 IP")
    if allowed_download_hosts and not any(
        hostname == allowed or hostname.endswith("." + allowed)
        for allowed in allowed_download_hosts
    ):
        raise ProviderCallError(f"{provider_id} 返回的音频地址不在允许域名内")
    return value


class MiniMaxMusicGenerationPort:
    """MiniMax Music 2.6 instrumental adapter with bounded media retrieval."""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str = "music-2.6",
        timeout_seconds: float = 300,
        max_bytes: int = 64 * 1024 * 1024,
        default_response_format: str = "hex",
        minimum_duration_seconds: float = 1,
        maximum_duration_seconds: float = 300,
        allowed_download_hosts: tuple[str, ...] = (),
    ):
        if default_response_format not in _MUSIC_RESPONSE_FORMATS:
            raise ValueError("MiniMax 配乐返回格式仅支持 hex 或 url")
        if minimum_duration_seconds <= 0 or maximum_duration_seconds < minimum_duration_seconds:
            raise ValueError("MiniMax 配乐时长范围无效")
        if max_bytes < 1024:
            raise ValueError("配乐最大字节数过小")
        self.endpoint = _provider_endpoint(base_url, "/v1/music_generation")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.default_response_format = default_response_format
        self.minimum_duration_seconds = minimum_duration_seconds
        self.maximum_duration_seconds = maximum_duration_seconds
        self.allowed_download_hosts = tuple(
            value.lower().strip().rstrip(".")
            for value in (allowed_download_hosts or _DEFAULT_MINIMAX_DOWNLOAD_HOSTS)
            if value.strip()
        )
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="minimax-music-2.6",
            capability="music-generation",
            mode="api",
            external=True,
            enabled=True,
            model=model,
        )
        self.capabilities = {
            "instrumental_only": True,
            "formats": sorted(_MUSIC_FORMATS),
            "response_formats": sorted(_MUSIC_RESPONSE_FORMATS),
            "sample_rates": sorted(_MINIMAX_MUSIC_SAMPLE_RATES),
            "bitrates": sorted(_MINIMAX_MUSIC_BITRATES),
            "duration_seconds": {
                "minimum": minimum_duration_seconds,
                "maximum": maximum_duration_seconds,
                "control": "timeline-trim-or-loop",
            },
            "returns_bytes": True,
            "temporary_urls_persisted_immediately": True,
            "download_host_allowlist": list(self.allowed_download_hosts),
            "parameter_source": "minimax-music-generation-contract",
        }

    def _download_audio(
        self,
        client: httpx.Client,
        url: str,
        audio_format: str,
    ) -> bytes:
        safe_url = _safe_supplier_audio_url(
            url,
            self.status.provider_id,
            self.allowed_download_hosts,
        )
        with client.stream("GET", safe_url) as response:
            response.raise_for_status()
            content_type = (
                str(response.headers.get("content-type", "application/octet-stream"))
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if content_type not in _MUSIC_CONTENT_TYPES[audio_format]:
                raise ProviderCallError(
                    f"{self.status.provider_id} 音频下载返回了不允许的 Content-Type：{content_type}"
                )
            return _bounded_response_bytes(
                response,
                max_bytes=self.max_bytes,
                provider_id=self.status.provider_id,
            )

    def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        prompt = _validate_music_request(
            request,
            minimum_duration_seconds=self.minimum_duration_seconds,
            maximum_duration_seconds=self.maximum_duration_seconds,
        )
        response_format = request.response_format or self.default_response_format
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "output_format": response_format,
            "audio_setting": {
                "sample_rate": request.sample_rate,
                "bitrate": request.bitrate,
                "format": request.audio_format,
            },
            "lyrics_optimizer": False,
            "is_instrumental": True,
            "aigc_watermark": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # A hex response is roughly twice the encoded audio size; leave bounded
        # headroom for the small JSON envelope while still rejecting oversized data.
        response_limit = self.max_bytes * 2 + 1024 * 1024
        try:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    self.endpoint,
                    headers=headers,
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    raw_response = _bounded_response_bytes(
                        response,
                        max_bytes=response_limit,
                        provider_id=self.status.provider_id,
                    )
                response_payload = json.loads(raw_response)
                if not isinstance(response_payload, dict):
                    raise TypeError("顶层不是 JSON 对象")
                base_response = response_payload.get("base_resp")
                if not isinstance(base_response, dict):
                    raise TypeError("缺少 base_resp")
                if base_response.get("status_code") != 0:
                    code = str(base_response.get("status_code") or "unknown")[:40]
                    message = " ".join(str(base_response.get("status_msg") or "").split())[:200]
                    message = re.sub(r"https?://\S+", "[url]", message)
                    raise ProviderCallError(
                        f"{self.status.provider_id} 生成失败：{code} / {message or 'unknown'}"
                    )
                data = response_payload.get("data")
                if not isinstance(data, dict) or data.get("status") != 2:
                    raise ProviderCallError(f"{self.status.provider_id} 未返回已完成的配乐")
                raw_audio = data.get("audio")
                if not isinstance(raw_audio, str) or not raw_audio.strip():
                    raise TypeError("缺少 data.audio")
                if response_format == "hex":
                    if len(raw_audio) > self.max_bytes * 2:
                        raise ProviderCallError(
                            f"{self.status.provider_id} 返回的十六进制音频超过允许大小"
                        )
                    try:
                        audio = binascii.unhexlify(raw_audio.strip())
                    except (binascii.Error, ValueError) as exc:
                        raise ProviderCallError(
                            f"{self.status.provider_id} 返回了无效的十六进制音频"
                        ) from exc
                else:
                    audio = self._download_audio(client, raw_audio.strip(), request.audio_format)
        except ProviderCallError:
            raise
        except httpx.HTTPError as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderCallError(f"{self.status.provider_id} 返回的配乐结果无效：{exc}") from exc

        _validate_audio_content(
            audio,
            request.audio_format,
            self.status.provider_id,
            max_bytes=self.max_bytes,
        )
        extra_info = response_payload.get("extra_info")
        if not isinstance(extra_info, dict):
            extra_info = {}
        raw_duration = extra_info.get("music_duration")
        duration = (
            float(raw_duration) / 1000
            if isinstance(raw_duration, (int, float)) and raw_duration >= 0
            else (_wav_duration(audio) if request.audio_format == "wav" else None)
        )
        return MusicGenerationResult(
            audio=audio,
            media_type=_MUSIC_FORMATS[request.audio_format],
            extension=request.audio_format,
            duration_seconds=duration,
            requested_duration_seconds=request.duration_seconds,
            provider_id=self.status.provider_id,
            model=self.model,
            metadata={
                "instrumental": True,
                "trace_id": str(response_payload.get("trace_id") or "")[:128] or None,
                "sample_rate": extra_info.get("music_sample_rate") or request.sample_rate,
                "channels": extra_info.get("music_channel"),
                "bitrate": extra_info.get("bitrate") or request.bitrate,
                "music_size": len(audio),
                "supplier_response_format": response_format,
                "duration_control": "timeline-trim-or-loop",
            },
        )


class MockSpeechSynthesisPort:
    capabilities: ClassVar[dict[str, Any]] = {
        "voices": ["mock-voice"],
        "formats": ["wav"],
        "speed": {"min": 0.1, "max": 2.0},
        "volume": {"min": 0.5, "max": 2.0},
        "timestamps": "estimated",
    }

    def __init__(
        self,
        *,
        provider_id: str = "mock-speech",
        default_voice: str = "mock-voice",
    ):
        self.default_voice = default_voice
        self.capabilities = {**type(self).capabilities, "voices": [default_voice]}
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="deterministic-speech",
            capability="speech-synthesis",
            mode="mock",
            external=False,
            enabled=True,
            model="sine-wave",
        )

    def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        if not request.text.strip():
            raise ValueError("配音文本不能为空")
        duration = min(max(len(request.text) / (8.0 * request.speed), 0.8), 120.0)
        sample_rate = request.sample_rate
        frames = int(duration * sample_rate)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            audio = bytearray()
            for frame in range(frames):
                value = int(900 * math.sin(2 * math.pi * 220 * frame / sample_rate))
                audio.extend(struct.pack("<h", value))
            wav.writeframes(audio)
        timings = _estimated_word_timings(request.text, duration)
        return SpeechSynthesisResult(
            audio=output.getvalue(),
            media_type="audio/wav",
            extension="wav",
            duration_seconds=duration,
            provider_id=self.status.provider_id,
            voice=request.voice or self.default_voice,
            word_timings=timings,
            metadata={"mock": True, "sample_rate": sample_rate},
        )


def _estimated_word_timings(text: str, duration: float) -> tuple[WordTiming, ...]:
    characters = [character for character in text if not character.isspace()]
    if not characters:
        return ()
    step = duration / len(characters)
    return tuple(
        WordTiming(character, index * step, min(duration, (index + 1) * step))
        for index, character in enumerate(characters)
    )


def _timestamp_seconds(value: Any, *, assume_seconds: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number < 0:
        return None
    # Legacy snake_case payloads commonly use milliseconds. The API-key TTS 2.0
    # camelCase contract is explicitly seconds, including for long recordings.
    return number if assume_seconds else (number / 1000 if number >= 100 else number)


def _collect_word_timings(value: Any) -> list[WordTiming]:
    results: list[WordTiming] = []
    if isinstance(value, dict):
        text = value.get("word", value.get("text"))
        uses_camel_case_seconds = "startTime" in value or "endTime" in value
        start = value.get("start_time", value.get("startTime", value.get("start")))
        end = value.get("end_time", value.get("endTime", value.get("end")))
        start_seconds = _timestamp_seconds(start, assume_seconds=uses_camel_case_seconds)
        end_seconds = _timestamp_seconds(end, assume_seconds=uses_camel_case_seconds)
        if (
            isinstance(text, str)
            and text
            and start_seconds is not None
            and end_seconds is not None
            and end_seconds >= start_seconds
        ):
            results.append(WordTiming(text, start_seconds, end_seconds))
        for key, nested in value.items():
            if key not in {
                "word",
                "text",
                "start_time",
                "startTime",
                "start",
                "end_time",
                "endTime",
                "end",
            }:
                results.extend(_collect_word_timings(nested))
    elif isinstance(value, list):
        for nested in value:
            results.extend(_collect_word_timings(nested))
    return results


def _audio_chunks_from_json(value: Any) -> list[bytes]:
    chunks: list[bytes] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"audio", "audio_data", "audioData"} and isinstance(nested, str):
                with contextlib.suppress(ValueError, TypeError):
                    chunks.append(base64.b64decode(nested, validate=True))
            elif key == "data" and isinstance(nested, str):
                # V3 chunk messages use data for Base64 audio; ignore ordinary text.
                try:
                    decoded = base64.b64decode(nested, validate=True)
                except (ValueError, TypeError):
                    decoded = b""
                if decoded:
                    chunks.append(decoded)
            else:
                chunks.extend(_audio_chunks_from_json(nested))
    elif isinstance(value, list):
        for nested in value:
            chunks.extend(_audio_chunks_from_json(nested))
    return chunks


def _parse_tts_payload_lines(lines: Sequence[str | bytes]) -> list[Any]:
    payloads: list[Any] = []
    for raw_line in lines:
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return payloads


def _decode_tts_payloads(
    payloads: Sequence[Any],
    provider_id: str,
    *,
    accepted_codes: set[int] | None = None,
) -> tuple[bytes, tuple[WordTiming, ...], dict[str, Any]]:
    if not payloads:
        raise ProviderCallError(f"{provider_id} 返回了无法解析的配音数据")
    if accepted_codes is not None:
        for payload in payloads:
            if not isinstance(payload, dict) or payload.get("code") is None:
                continue
            try:
                code = int(payload["code"])
            except (TypeError, ValueError) as exc:
                raise ProviderCallError(f"{provider_id} 返回了无效的业务状态码") from exc
            if code not in accepted_codes:
                raise ProviderCallError(f"{provider_id} 配音生成失败（业务状态 {code}）")
    chunks: list[bytes] = []
    timings: list[WordTiming] = []
    for payload in payloads:
        chunks.extend(_audio_chunks_from_json(payload))
        timings.extend(_collect_word_timings(payload))
    if not chunks:
        message = next(
            (
                item.get("message")
                for item in payloads
                if isinstance(item, dict) and isinstance(item.get("message"), str)
            ),
            "响应中缺少音频字段",
        )
        raise ProviderCallError(f"{provider_id} 配音生成失败：{message}")
    unique_timings = tuple(
        sorted(
            dict.fromkeys((item.text, item.start_seconds, item.end_seconds) for item in timings),
            key=lambda item: (item[1], item[2], item[0]),
        )
    )
    word_timings = tuple(WordTiming(*item) for item in unique_timings)
    return b"".join(chunks), word_timings, {"chunk_count": len(chunks)}


def _decode_tts_response(
    response: httpx.Response,
    provider_id: str,
    *,
    accepted_codes: set[int] | None = None,
) -> tuple[bytes, tuple[WordTiming, ...], dict[str, Any]]:
    content = bytes(response.content)
    content_type = str(response.headers.get("content-type", "")).lower()
    if content_type.startswith("audio/"):
        return content, (), {}
    try:
        decoded_text = content.decode("utf-8")
    except UnicodeDecodeError:
        # Some legacy deployments return encoded audio directly over HTTP chunking.
        if content:
            return content, (), {}
        raise ProviderCallError(f"{provider_id} 返回了空配音数据") from None
    payloads = _parse_tts_payload_lines(decoded_text.splitlines() or [decoded_text])
    return _decode_tts_payloads(
        payloads,
        provider_id,
        accepted_codes=accepted_codes,
    )


def _wav_duration(content: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(content), "rb") as wav:
            return wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def _pcm_to_wav(content: bytes, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(content)
    return output.getvalue()


def _ratio_to_provider_rate(value: float) -> int:
    return max(-50, min(100, round((value - 1) * 100)))


class VolcengineApiKeySpeechSynthesisPort:
    """Doubao Speech TTS 2.0 API-key adapter owned by the content service."""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        resource_id: str,
        default_voice: str,
        timeout_seconds: float,
        supported_voices: tuple[str, ...] = (),
    ):
        self.endpoint = _provider_endpoint(base_url, "/api/v3/tts/unidirectional")
        self.api_key = api_key
        self.resource_id = resource_id
        self.default_voice = default_voice
        self.timeout_seconds = timeout_seconds
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="volcengine-doubao-speech-tts-2",
            capability="speech-synthesis",
            mode="api",
            external=True,
            enabled=True,
            model=resource_id,
        )
        self.capabilities = {
            "voices": list(supported_voices or (default_voice,)),
            "voice_labels": {
                v: {
                    "aiden": "艾登",
                    "dylan": "迪伦",
                    "eric": "埃里克",
                    "ono_anna": "安娜",
                    "ryan": "瑞安",
                    "serena": "塞琳娜",
                    "sohee": "素熙",
                    "uncle_fu": "傅叔",
                    "vivian": "薇薇安",
                }.get(v, v.replace("_", " ").title())
                for v in (supported_voices or (default_voice,))
            },
            "formats": ["wav", "mp3", "pcm", "ogg_opus"],
            "speed": {"min": 0.5, "max": 2.0},
            "volume": {"min": 0.5, "max": 2.0},
            "timestamps": "provider-word-level",
            "timestamp_languages": ["zh", "en"],
            "parameter_source": "provider-contract-and-service-configuration",
        }

    def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        if not request.text.strip():
            raise ValueError("配音文本不能为空")
        if len(request.text.encode("utf-8")) > 1024:
            raise ValueError("单次配音文本不能超过 1024 UTF-8 字节")
        if not 0.5 <= request.speed <= 2:
            raise ValueError("新版配音语速必须在 0.5 到 2 之间")
        if not 0.5 <= request.volume <= 2:
            raise ValueError("新版配音音量必须在 0.5 到 2 之间")
        if request.sample_rate <= 0:
            raise ValueError("配音采样率必须大于 0")

        voice = request.voice or self.default_voice
        provider_format = "pcm" if request.audio_format == "wav" else request.audio_format
        audio_params: dict[str, Any] = {
            "format": provider_format,
            "sample_rate": request.sample_rate,
            "speech_rate": _ratio_to_provider_rate(request.speed),
            "loudness_rate": _ratio_to_provider_rate(request.volume),
            "enable_subtitle": bool(request.with_timestamps),
        }
        configured_audio_params = request.extra.get("audio_params")
        if isinstance(configured_audio_params, dict):
            for name in ("bit_rate", "emotion", "emotion_scale"):
                if name in configured_audio_params:
                    audio_params[name] = configured_audio_params[name]
        if provider_format == "mp3" and "bit_rate" not in audio_params:
            audio_params["bit_rate"] = 64000

        req_params: dict[str, Any] = {
            "text": request.text,
            "speaker": voice,
            "audio_params": audio_params,
        }
        additions = {
            "disable_markdown_filter": True,
            "enable_latex_tn": False,
        }
        configured_additions = request.extra.get("additions")
        if isinstance(configured_additions, dict):
            additions.update(configured_additions)
        req_params["additions"] = json.dumps(
            additions,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = {
            "user": {"uid": request.user_id},
            "req_params": req_params,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid4()),
            "X-Control-Require-Usage-Tokens-Return": "*",
        }

        try:
            with (
                httpx.Client(timeout=self.timeout_seconds) as client,
                client.stream("POST", self.endpoint, headers=headers, json=payload) as response,
            ):
                response.raise_for_status()
                payloads = _parse_tts_payload_lines(tuple(response.iter_lines()))
                audio, timings, metadata = _decode_tts_payloads(
                    payloads,
                    self.status.provider_id,
                    accepted_codes={0, 20000000},
                )
                log_id = response.headers.get("X-Tt-Logid")
        except httpx.HTTPError as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc

        extension = request.audio_format
        if extension == "wav":
            audio = _pcm_to_wav(audio, request.sample_rate)
        duration = timings[-1].end_seconds if timings else None
        if extension == "wav" and duration is None:
            duration = _wav_duration(audio)
        elif extension == "pcm" and duration is None:
            duration = len(audio) / (request.sample_rate * 2)
        return SpeechSynthesisResult(
            audio=audio,
            media_type=_MEDIA_TYPES[extension],
            extension=extension,
            duration_seconds=duration,
            provider_id=self.status.provider_id,
            voice=voice,
            word_timings=timings,
            metadata={
                **metadata,
                "resource_id": self.resource_id,
                "provider_log_id": log_id,
                "timestamp_source": "provider" if timings else "unavailable",
                "protocol": "doubao-speech-api-key-v3-chunked",
            },
        )


class OpenAICompatibleSpeechSynthesisPort:
    """OpenAI-compatible TTS adapter for the chat-tts model."""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str,
        default_voice: str,
        timeout_seconds: float,
        supported_voices: tuple[str, ...] = (),
    ):
        self.endpoint = _provider_endpoint(base_url, "/v1/audio/speech")
        self.api_key, self.model, self.default_voice = api_key, model, default_voice
        self.timeout_seconds = timeout_seconds
        self.status = ProviderStatus(
            provider_id, "chat-tts", "speech-synthesis", "api", True, True, model
        )
        self.capabilities = {
            "voices": list(supported_voices or (default_voice,)),
            "formats": ["wav", "mp3"],
            "speed": {"min": 0.5, "max": 2.0},
            "volume": {"min": 0.5, "max": 2.0},
            "timestamps": "unavailable",
            "parameter_source": "chat-tts-openai-compatible",
        }

    def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        if not request.text.strip():
            raise ValueError("配音文本不能为空")
        if request.audio_format not in {"wav", "mp3"}:
            raise ValueError("chat-tts 仅支持 WAV 或 MP3")
        voice = request.voice or self.default_voice
        if voice not in self.capabilities["voices"]:
            raise ValueError(f"不支持的音色：{voice}")
        payload = {
            "model": self.model,
            "input": request.text,
            "voice": voice,
            "response_format": request.audio_format,
        }
        if request.speed != 1.0:
            payload["speed"] = request.speed
        try:
            response = httpx.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            audio = response.content
        except httpx.HTTPError as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        return SpeechSynthesisResult(
            audio=audio,
            media_type=_MEDIA_TYPES[request.audio_format],
            extension=request.audio_format,
            duration_seconds=_wav_duration(audio) if request.audio_format == "wav" else None,
            provider_id=self.status.provider_id,
            voice=voice,
            metadata={
                "protocol": "openai-compatible-audio-speech",
                "model": self.model,
                "timestamp_source": "unavailable",
            },
        )


class VolcengineSpeechSynthesisPort:
    """Volcengine V3 HTTP Chunked TTS; credentials never leave the service process."""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        app_id: str,
        access_token: str,
        resource_id: str,
        default_voice: str,
        timeout_seconds: float,
        supported_voices: tuple[str, ...] = (),
        cluster: str = "volcano_tts",
    ):
        self.endpoint = _provider_endpoint(base_url, "/api/v3/tts/unidirectional")
        self.app_id = app_id
        self.access_token = access_token
        self.resource_id = resource_id
        self.cluster = cluster
        self.default_voice = default_voice
        self.timeout_seconds = timeout_seconds
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="volcengine-v3-tts",
            capability="speech-synthesis",
            mode="api",
            external=True,
            enabled=True,
            model=resource_id,
        )
        self.capabilities = {
            "voices": list(supported_voices or (default_voice,)),
            "formats": ["wav", "mp3", "pcm", "ogg_opus"],
            "speed": {"min": 0.1, "max": 2.0},
            "volume": {"min": 0.5, "max": 2.0},
            "timestamps": "provider-word-level",
            "parameter_source": "provider-contract-and-service-configuration",
        }

    def synthesize(self, request: SpeechSynthesisRequest) -> SpeechSynthesisResult:
        if not request.text.strip():
            raise ValueError("配音文本不能为空")
        if len(request.text.encode("utf-8")) > 1024:
            raise ValueError("单次配音文本不能超过 1024 UTF-8 字节")
        if not 0.1 <= request.speed <= 2:
            raise ValueError("语速必须在 0.1 到 2 之间")
        if not 0.5 <= request.volume <= 2:
            raise ValueError("音量必须在 0.5 到 2 之间")
        voice = request.voice or self.default_voice
        payload: dict[str, Any] = {
            "app": {
                "appid": self.app_id,
                "token": self.access_token,
                "cluster": self.cluster,
            },
            "user": {"uid": request.user_id},
            "audio": {
                "voice_type": voice,
                "encoding": request.audio_format,
                "speed_ratio": request.speed,
                "loudness_ratio": request.volume,
                "rate": request.sample_rate,
            },
            "request": {
                "reqid": str(uuid4()),
                "text": request.text,
                "operation": "query",
                "with_timestamp": 1 if request.with_timestamps else 0,
            },
        }
        for key, value in request.extra.items():
            if key in {"app", "audio", "request", "user"} and isinstance(value, dict):
                payload[key].update(value)
            elif key not in payload:
                payload[key] = value
        headers = {
            "Authorization": f"Bearer; {self.access_token}",
            "Resource-Id": self.resource_id,
            "X-Api-Resource-Id": self.resource_id,
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(self.endpoint, headers=headers, json=payload)
                response.raise_for_status()
                audio, timings, metadata = _decode_tts_response(
                    response,
                    self.status.provider_id,
                )
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        extension = request.audio_format
        duration = timings[-1].end_seconds if timings else None
        if extension == "wav" and duration is None:
            duration = _wav_duration(audio)
        return SpeechSynthesisResult(
            audio=audio,
            media_type=_MEDIA_TYPES[extension],
            extension=extension,
            duration_seconds=duration,
            provider_id=self.status.provider_id,
            voice=voice,
            word_timings=timings,
            metadata={
                **metadata,
                "resource_id": self.resource_id,
                "timestamp_source": "provider" if timings else "unavailable",
            },
        )


def _split_subtitle_text(text: str, max_chars: int) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    segments: list[str] = []
    current = ""
    for character in normalized:
        current += character
        visible_length = len(current.replace(" ", ""))
        if character in "。！？!?；;\n" or visible_length >= max_chars:
            segments.append(current.strip())
            current = ""
    if current.strip():
        segments.append(current.strip())
    return segments


class LocalSubtitleAlignmentPort:
    def __init__(self, *, default_max_chars_per_line: int = 24, default_max_lines: int = 2):
        if default_max_chars_per_line < 1 or default_max_lines < 1:
            raise ValueError("字幕分段默认参数必须大于 0")
        self.default_max_chars_per_line = default_max_chars_per_line
        self.default_max_lines = default_max_lines
        self.status = ProviderStatus(
            provider_id="local-subtitle-alignment",
            name="timestamp-aware-subtitle-alignment",
            capability="subtitle-alignment",
            mode="local",
            external=False,
            enabled=True,
            model="word-timing-or-estimator-v1",
        )
        self.capabilities = {
            "default_max_chars_per_line": default_max_chars_per_line,
            "default_max_lines": default_max_lines,
            "timing_sources": ["provider", "estimated"],
            "parameter_source": "service-configuration",
        }

    def align(self, request: SubtitleAlignmentRequest) -> Sequence[SubtitleSegment]:
        if not request.text.strip():
            raise ValueError("字幕文本不能为空")
        if request.duration_seconds <= 0:
            raise ValueError("字幕时长必须大于 0")
        max_chars_per_line = (
            request.max_chars_per_line
            if request.max_chars_per_line is not None
            else self.default_max_chars_per_line
        )
        max_lines = request.max_lines if request.max_lines is not None else self.default_max_lines
        max_chars = max_chars_per_line * max_lines
        if max_chars < 1:
            raise ValueError("字幕每段最大字符数必须大于 0")
        if request.word_timings:
            return self._from_provider_timings(request, max_chars)
        values = _split_subtitle_text(request.text, max_chars)
        total_weight = sum(max(1, len(value.replace(" ", ""))) for value in values)
        cursor = 0.0
        results: list[SubtitleSegment] = []
        for index, value in enumerate(values):
            weight = max(1, len(value.replace(" ", "")))
            end = (
                request.duration_seconds
                if index == len(values) - 1
                else cursor + request.duration_seconds * weight / total_weight
            )
            results.append(SubtitleSegment(index + 1, value, cursor, end, "estimated"))
            cursor = end
        return results

    @staticmethod
    def _from_provider_timings(
        request: SubtitleAlignmentRequest,
        max_chars: int,
    ) -> Sequence[SubtitleSegment]:
        timings = sorted(request.word_timings, key=lambda item: item.start_seconds)
        results: list[SubtitleSegment] = []
        current: list[WordTiming] = []
        current_length = 0
        for timing in timings:
            text = timing.text.strip()
            if not text:
                continue
            current.append(timing)
            current_length += len(text)
            if text[-1:] in "。！？!?；;" or current_length >= max_chars:
                results.append(
                    SubtitleSegment(
                        len(results) + 1,
                        "".join(item.text for item in current),
                        max(0.0, current[0].start_seconds),
                        min(request.duration_seconds, current[-1].end_seconds),
                        "provider",
                    )
                )
                current = []
                current_length = 0
        if current:
            results.append(
                SubtitleSegment(
                    len(results) + 1,
                    "".join(item.text for item in current),
                    max(0.0, current[0].start_seconds),
                    min(request.duration_seconds, current[-1].end_seconds),
                    "provider",
                )
            )
        previous_end = 0.0
        normalized: list[SubtitleSegment] = []
        for segment in results:
            start = max(previous_end, segment.start_seconds)
            end = max(start + 0.001, segment.end_seconds)
            end = min(request.duration_seconds, end)
            if end <= start:
                continue
            normalized.append(
                SubtitleSegment(
                    len(normalized) + 1,
                    segment.text,
                    start,
                    end,
                    "provider",
                )
            )
            previous_end = end
        return normalized


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    return (
        f"{milliseconds // 3_600_000:02}:"
        f"{(milliseconds // 60_000) % 60:02}:"
        f"{(milliseconds // 1_000) % 60:02},"
        f"{milliseconds % 1_000:03}"
    )


def render_srt(segments: Sequence[SubtitleSegment]) -> str:
    blocks = [
        f"{index}\n{_srt_timestamp(segment.start_seconds)} --> "
        f"{_srt_timestamp(segment.end_seconds)}\n{segment.text}"
        for index, segment in enumerate(segments, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _validate_ai_video_reference_url(
    value: str,
    *,
    kind: str,
    allow_internal_asset_url: bool = False,
) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if kind == "image" and parsed.scheme.lower() == "data":
        if not normalized.lower().startswith("data:image/") or ";base64," not in normalized[:128]:
            raise ValueError("参考图片 Data URL 必须包含 image 媒体类型和 Base64 编码")
        return normalized
    if kind == "video" and allow_internal_asset_url and parsed.scheme.lower() == "asset":
        if not parsed.netloc and not parsed.path:
            raise ValueError("内部参考视频 Asset URL 不能为空")
        return normalized
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        label = "图片" if kind == "image" else "视频"
        raise ValueError(f"参考{label}必须使用无凭据、无片段的 HTTPS URL")
    return normalized


def _ai_video_references(
    request: AiVideoGenerationRequest,
    *,
    allow_internal_asset_urls: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    image_urls = (
        *((request.image_url,) if request.image_url else ()),
        *request.reference_image_urls,
    )
    video_urls = request.reference_video_urls
    if len(image_urls) > _MAX_AI_VIDEO_REFERENCE_IMAGES:
        raise ValueError(f"AI 视频单次最多允许 {_MAX_AI_VIDEO_REFERENCE_IMAGES} 张参考图片")
    if len(video_urls) > _MAX_AI_VIDEO_REFERENCE_VIDEOS:
        raise ValueError(f"AI 视频单次最多允许 {_MAX_AI_VIDEO_REFERENCE_VIDEOS} 段参考视频")
    if len(image_urls) != len(set(image_urls)):
        raise ValueError("AI 视频参考图片不能重复")
    if len(video_urls) != len(set(video_urls)):
        raise ValueError("AI 视频参考视频不能重复")
    if request.reference_video_durations_seconds and len(
        request.reference_video_durations_seconds
    ) != len(video_urls):
        raise ValueError("参考视频与时长数量不一致")
    known_durations = tuple(
        float(value) for value in request.reference_video_durations_seconds if value is not None
    )
    if any(
        not math.isfinite(value)
        or not (
            _MIN_AI_VIDEO_REFERENCE_VIDEO_SECONDS <= value <= _MAX_AI_VIDEO_REFERENCE_VIDEO_SECONDS
        )
        for value in known_durations
    ):
        raise ValueError("每段参考视频时长必须在 2 到 15 秒之间")
    if sum(known_durations) > _MAX_AI_VIDEO_REFERENCE_VIDEO_TOTAL_SECONDS:
        raise ValueError("参考视频总时长不能超过 15 秒")
    return (
        tuple(_validate_ai_video_reference_url(value, kind="image") for value in image_urls),
        tuple(
            _validate_ai_video_reference_url(
                value,
                kind="video",
                allow_internal_asset_url=allow_internal_asset_urls,
            )
            for value in video_urls
        ),
    )


def _ai_video_reference_capabilities() -> dict[str, Any]:
    return {
        "images": {
            "max": _MAX_AI_VIDEO_REFERENCE_IMAGES,
            "formats": ["image/jpeg", "image/png", "image/webp"],
            "max_bytes_per_file": 30 * 1024 * 1024,
            "role": "reference_image",
            "transports": ["image-data-url", "https-url"],
        },
        "videos": {
            "max": _MAX_AI_VIDEO_REFERENCE_VIDEOS,
            "formats": ["video/mp4", "video/quicktime"],
            "max_bytes_per_file": 200 * 1024 * 1024,
            "min_duration_seconds": _MIN_AI_VIDEO_REFERENCE_VIDEO_SECONDS,
            "max_duration_seconds": _MAX_AI_VIDEO_REFERENCE_VIDEO_SECONDS,
            "total_duration_seconds": _MAX_AI_VIDEO_REFERENCE_VIDEO_TOTAL_SECONDS,
            "max_total_duration_seconds": _MAX_AI_VIDEO_REFERENCE_VIDEO_TOTAL_SECONDS,
            "role": "reference_video",
            "transports": ["https-url"],
            "requires_provider_fetchable_url": True,
        },
    }


class MockAiVideoGenerationPort(AiVideoGenerationPort):
    def __init__(
        self,
        *,
        provider_id: str = "mock-ai-video",
        model: str = "deterministic-video-task",
        supported_durations: tuple[int, ...] = (5, 10),
        supported_ratios: tuple[str, ...] = ("16:9", "9:16", "1:1"),
        supported_resolutions: tuple[str, ...] = ("480p", "720p"),
    ):
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="deterministic-ai-video-task",
            capability="ai-video-generation",
            mode="mock",
            external=False,
            enabled=True,
            model=model,
        )
        reference_media = _ai_video_reference_capabilities()
        self.capabilities = {
            "async": True,
            "image_to_video": True,
            "text_to_video": True,
            "reference_images": reference_media["images"],
            "reference_videos": reference_media["videos"],
            "reference_media": reference_media,
            "durations": list(supported_durations),
            "ratios": list(supported_ratios),
            "resolutions": list(supported_resolutions),
            "generate_audio": True,
            "return_last_frame": True,
            "parameter_source": "service-configuration",
            "mock": True,
        }
        self._tasks: dict[str, AiVideoTask] = {}
        self._lock = threading.Lock()

    def submit(self, request: AiVideoGenerationRequest) -> AiVideoTask:
        if not request.prompt.strip():
            raise ValueError("视频提示词不能为空")
        _ai_video_references(request, allow_internal_asset_urls=True)
        task_id = uuid4().hex
        task = AiVideoTask(
            provider_task_id=task_id,
            status="queued",
            provider_id=self.status.provider_id,
            model=self.status.model or "",
            progress=0,
            metadata={"mock": True, "prompt": request.prompt},
        )
        with self._lock:
            self._tasks[task_id] = task
        return task

    def get(self, provider_task_id: str) -> AiVideoTask:
        with self._lock:
            task = self._tasks.get(provider_task_id)
            if task is None:
                raise ProviderCallError("mock-ai-video 任务不存在")
            if task.status == "queued":
                task = AiVideoTask(
                    provider_task_id=task.provider_task_id,
                    status="succeeded",
                    provider_id=task.provider_id,
                    model=task.model,
                    progress=100,
                    video_url=f"mock://video/{task.provider_task_id}",
                    metadata=task.metadata,
                )
                self._tasks[provider_task_id] = task
            return task

    def cancel(self, provider_task_id: str) -> AiVideoTask:
        with self._lock:
            task = self._tasks.get(provider_task_id)
            if task is None:
                raise ProviderCallError("mock-ai-video 任务不存在")
            if task.status not in _TERMINAL_VIDEO_STATUSES:
                task = AiVideoTask(
                    provider_task_id=task.provider_task_id,
                    status="cancelled",
                    provider_id=task.provider_id,
                    model=task.model,
                    progress=task.progress,
                    metadata=task.metadata,
                )
                self._tasks[provider_task_id] = task
            return task


class VolcengineArkAiVideoGenerationPort(AiVideoGenerationPort):
    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        supported_durations: tuple[int, ...] = (),
        supported_ratios: tuple[str, ...] = (),
        supported_resolutions: tuple[str, ...] = (),
    ):
        self.endpoint = _provider_endpoint(base_url, "/contents/generations/tasks")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="volcengine-ark-ai-video",
            capability="ai-video-generation",
            mode="async-api",
            external=True,
            enabled=True,
            model=model,
        )
        reference_media = _ai_video_reference_capabilities()
        reference_media["videos"] = {
            **reference_media["videos"],
            "provider_native_reference_supported": True,
            "provider_native_reference_transport": PROVIDER_NATIVE_VIDEO_TRANSPORT,
        }
        self.capabilities = {
            "async": True,
            "text_to_video": True,
            "image_to_video": True,
            "reference_images": reference_media["images"],
            "reference_videos": reference_media["videos"],
            "reference_media": reference_media,
            "durations": list(supported_durations),
            "ratios": list(supported_ratios),
            "resolutions": list(supported_resolutions),
            "generate_audio": True,
            "return_last_frame": True,
            "parameter_source": "service-configuration",
        }

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _task(self, payload: Any, fallback_task_id: str | None = None) -> AiVideoTask:
        if not isinstance(payload, dict):
            raise ProviderCallError(f"{self.status.provider_id} 返回的任务结构无效")
        task_id = payload.get("id", payload.get("task_id", fallback_task_id))
        status = str(payload.get("status", "queued")).lower()
        if not isinstance(task_id, str) or not task_id:
            raise ProviderCallError(f"{self.status.provider_id} 返回结果缺少任务 ID")
        if status not in _ARK_VIDEO_STATUSES:
            raise ProviderCallError(f"{self.status.provider_id} 返回未知任务状态 {status}")
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        progress = payload.get("progress")
        if isinstance(progress, float) and 0 <= progress <= 1:
            progress = round(progress * 100)
        if not isinstance(progress, int):
            progress = None
        return AiVideoTask(
            provider_task_id=task_id,
            status=status,  # type: ignore[arg-type]
            provider_id=self.status.provider_id,
            model=str(payload.get("model") or self.model),
            progress=progress,
            video_url=content.get("video_url") or payload.get("video_url"),
            last_frame_url=content.get("last_frame_url") or payload.get("last_frame_url"),
            error_code=str(error.get("code")) if error.get("code") is not None else None,
            error_message=(
                str(error.get("message"))[:500] if error.get("message") is not None else None
            ),
            metadata={
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
                "usage": payload.get("usage", {}),
            },
        )

    def submit(self, request: AiVideoGenerationRequest) -> AiVideoTask:
        if not request.prompt.strip():
            raise ValueError("视频提示词不能为空")
        image_urls, video_urls = _ai_video_references(request)
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        image_role = (
            "first_frame"
            if request.first_frame and len(image_urls) == 1 and not video_urls
            else "reference_image"
        )
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": reference_url},
                "role": image_role,
            }
            for reference_url in image_urls
        )
        content.extend(
            {
                "type": "video_url",
                "video_url": {"url": reference_url},
                "role": "reference_video",
            }
            for reference_url in video_urls
        )
        payload: dict[str, Any] = {"model": self.model, "content": content}
        if request.callback_url:
            payload["callback_url"] = request.callback_url
        if request.return_last_frame:
            payload["return_last_frame"] = True
        if request.generate_audio:
            payload["generate_audio"] = True
        if request.duration_seconds is not None:
            payload["duration"] = request.duration_seconds
        if request.ratio:
            payload["ratio"] = request.ratio
        if request.resolution:
            payload["resolution"] = request.resolution
        payload.update(request.extra)
        serialized_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if serialized_size > _MAX_ARK_AI_VIDEO_REQUEST_BODY_BYTES:
            raise ValueError("AI 视频请求体不能超过 64MB，请减少或压缩参考图片")
        headers = dict(self._headers)
        if request.idempotency_key:
            headers["Idempotency-Key"] = request.idempotency_key
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(self.endpoint, headers=headers, json=payload)
                response.raise_for_status()
                return self._task(response.json())
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except ValueError as exc:
            raise ProviderCallError(f"{self.status.provider_id} 返回的任务响应不是 JSON") from exc

    def get(self, provider_task_id: str) -> AiVideoTask:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.get(
                    f"{self.endpoint}/{provider_task_id}",
                    headers=self._headers,
                )
                response.raise_for_status()
                return self._task(response.json(), provider_task_id)
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except ValueError as exc:
            raise ProviderCallError(f"{self.status.provider_id} 返回的任务响应不是 JSON") from exc

    def cancel(self, provider_task_id: str) -> AiVideoTask:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.delete(
                    f"{self.endpoint}/{provider_task_id}",
                    headers=self._headers,
                )
                response.raise_for_status()
                if response.content:
                    return self._task(response.json(), provider_task_id)
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except ValueError as exc:
            raise ProviderCallError(f"{self.status.provider_id} 返回的取消响应不是 JSON") from exc
        return AiVideoTask(
            provider_task_id=provider_task_id,
            status="cancelled",
            provider_id=self.status.provider_id,
            model=self.model,
        )


def wait_for_ai_video_task(
    provider: AiVideoGenerationPort,
    provider_task_id: str,
    *,
    poll_interval_seconds: float,
    max_wait_seconds: float,
    on_update: Callable[[AiVideoTask], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AiVideoTask:
    if poll_interval_seconds <= 0 or max_wait_seconds <= 0:
        raise ValueError("AI 视频轮询间隔和最大等待时间必须大于 0")
    deadline = monotonic() + max_wait_seconds
    while True:
        task = provider.get(provider_task_id)
        if on_update:
            on_update(task)
        if task.status in _TERMINAL_VIDEO_STATUSES:
            if task.status == "succeeded" and not task.video_url:
                raise ProviderCallError(
                    f"{provider.status.provider_id} 视频任务成功但未返回视频地址"
                )
            return task
        if monotonic() >= deadline:
            raise TimeoutError(f"等待 {provider.status.provider_id} 视频任务超时")
        sleep(min(poll_interval_seconds, max(0.0, deadline - monotonic())))


def download_generated_video(
    task_or_url: AiVideoTask | str,
    *,
    asset_store: AssetStorePort,
    storage_key: str,
    timeout_seconds: float = 120,
    max_bytes: int = 512 * 1024 * 1024,
    trusted_download_prefixes: tuple[str, ...] | None = None,
) -> AssetWriteResult:
    """Transfer an expiring supplier result into service-owned storage."""

    if timeout_seconds <= 0 or max_bytes <= 0:
        raise ValueError("视频下载超时和最大字节数必须大于 0")
    if isinstance(task_or_url, AiVideoTask):
        if task_or_url.status != "succeeded" or not task_or_url.video_url:
            raise ValueError("只有已成功且包含地址的 AI 视频任务可以转存")
        url = task_or_url.video_url
    else:
        url = task_or_url
    # Chat supplies a concrete policy (including () for public-only suppliers).
    # Existing pipeline callers retain their download contract when omitted.
    if trusted_download_prefixes is not None:
        url = safe_result_download_url(
            url, trusted_prefixes=trusted_download_prefixes, media_name="视频"
        )
    parsed = urlsplit(url)
    if parsed.scheme == "mock":
        raise ProviderCallError("Mock AI 视频任务没有可下载的真实媒体")
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProviderCallError("AI 视频结果必须使用有效的 HTTPS 地址")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ProviderCallError("AI 视频结果地址不能指向本机")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if trusted_download_prefixes is None and address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise ProviderCallError("AI 视频结果地址不能指向非公网 IP")
    try:
        with (
            httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=False,
                **hiagent_download_httpx_options(url),
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            content_type = (
                str(response.headers.get("content-type", "application/octet-stream"))
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if not (
                content_type.startswith("video/") or content_type == "application/octet-stream"
            ):
                raise ProviderCallError(f"AI 视频下载返回了不允许的 Content-Type：{content_type}")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise ProviderCallError("AI 视频下载返回无效 Content-Length") from exc
                if declared_size > max_bytes:
                    raise ProviderCallError("AI 视频下载超过允许大小")
            total = 0
            with tempfile.SpooledTemporaryFile(max_size=min(max_bytes, 16 * 1024 * 1024)) as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ProviderCallError("AI 视频下载超过允许大小")
                    output.write(chunk)
                if total == 0:
                    raise ProviderCallError("AI 视频下载结果为空")
                output.seek(0)
                return asset_store.put_stream(
                    storage_key,
                    output,
                    content_type,
                )
    except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
        raise _safe_http_error("ai-video-download", exc) from exc


def _ffmpeg_executable(configured: str | None = None) -> str:
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError("CONTENT_FFMPEG_PATH 指向的文件不存在")
        return str(path)
    if found := shutil.which("ffmpeg"):
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _ffprobe_executable(ffmpeg: str) -> str | None:
    candidate = Path(ffmpeg).with_name(
        "ffprobe.exe" if Path(ffmpeg).suffix.lower() == ".exe" else "ffprobe"
    )
    if candidate.is_file():
        return str(candidate)
    return shutil.which("ffprobe")


def _probe_duration(path: Path, ffmpeg: str) -> float:
    ffprobe = _ffprobe_executable(ffmpeg)
    if not ffprobe:
        raise ValueError(f"{path.name} 未指定时长，且系统没有可用的 ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise ValueError(f"无法读取 {path.name} 的有效时长")
    return duration


def _subtitle_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    return value.replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


_TRANSITIONS = {
    "fade": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
}
_FIT_MODES = {"contain", "cover"}
_LOGO_POSITIONS = {
    "top_left": "24:24",
    "top_right": "W-w-24:24",
    "bottom_left": "24:H-h-24",
    "bottom_right": "W-w-24:H-h-24",
}
_SUBTITLE_STYLE_KEYS = {
    "font_size",
    "primary_color",
    "outline_color",
    "outline_width",
    "shadow",
    "alignment",
    "margin_v",
    "bold",
}


def _ass_color(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", normalized):
        raise ValueError(f"{field_name} 必须是 #RRGGBB 颜色值")
    red, green, blue = normalized[1:3], normalized[3:5], normalized[5:7]
    return f"&H00{blue}{green}{red}".upper()


def _subtitle_force_style(style: dict[str, Any]) -> str:
    if not style:
        return ""
    unknown = set(style).difference(_SUBTITLE_STYLE_KEYS)
    if unknown:
        raise ValueError(f"不支持的字幕样式参数：{', '.join(sorted(unknown))}")

    font_size = int(style.get("font_size", 36))
    outline_width = float(style.get("outline_width", 2))
    shadow = float(style.get("shadow", 0))
    alignment = int(style.get("alignment", 2))
    margin_v = int(style.get("margin_v", 36))
    if not 12 <= font_size <= 120:
        raise ValueError("字幕字号必须在 12 到 120 之间")
    if not 0 <= outline_width <= 10 or not 0 <= shadow <= 10:
        raise ValueError("字幕描边或阴影参数超出安全范围")
    if alignment not in range(1, 10):
        raise ValueError("字幕位置必须是 1 到 9")
    if not 0 <= margin_v <= 500:
        raise ValueError("字幕垂直边距必须在 0 到 500 之间")
    bold_value = style.get("bold", False)
    if not isinstance(bold_value, bool):
        raise TypeError("字幕粗体参数必须是布尔值")
    return ",".join(
        [
            f"FontSize={font_size}",
            f"PrimaryColour={_ass_color(style.get('primary_color', '#FFFFFF'), '字幕颜色')}",
            f"OutlineColour={_ass_color(style.get('outline_color', '#000000'), '字幕描边颜色')}",
            f"Outline={outline_width:g}",
            f"Shadow={shadow:g}",
            f"Alignment={alignment}",
            f"MarginV={margin_v}",
            f"Bold={-1 if bold_value else 0}",
        ]
    )


class FfmpegVideoComposerPort:
    def __init__(self, *, ffmpeg_path: str | None = None, timeout_seconds: float = 600):
        self.ffmpeg = _ffmpeg_executable(ffmpeg_path)
        self.timeout_seconds = timeout_seconds
        self.status = ProviderStatus(
            provider_id="local-ffmpeg",
            name="ffmpeg-video-composer",
            capability="video-composition",
            mode="local",
            external=False,
            enabled=True,
            model=None,
        )
        self.capabilities = {
            "transitions": ["none", *_TRANSITIONS.keys()],
            "transition_duration_seconds": {"minimum": 0, "maximum": 5, "default": 0.4},
            "fit_modes": sorted(_FIT_MODES),
            "source_audio_policy": {
                "supported": ["mute", "replace_by_voice"],
                "default": "mute",
                "keep_supported": False,
            },
            "subtitles": "burn-in-srt",
            "subtitle_style": {
                "font_size": {"minimum": 12, "maximum": 120, "default": 36},
                "colors": "#RRGGBB",
                "alignment": list(range(1, 10)),
            },
            "voice": True,
            "background_music": True,
            "background_music_volume": {"minimum": 0, "maximum": 2, "default": 0.18},
            "logo": True,
            "logo_positions": list(_LOGO_POSITIONS),
            "intro_outro": True,
            "custom_size": True,
        }

    def _with_intro_outro(self, request: VideoCompositionRequest) -> list[VideoClip]:
        clips = list(request.clips)
        if request.intro_path:
            duration = request.intro_duration_seconds or _probe_duration(
                request.intro_path,
                self.ffmpeg,
            )
            clips.insert(
                0,
                VideoClip(
                    request.intro_path,
                    duration,
                    "none",
                    is_image=False,
                    fit_mode=request.fit_mode,
                ),
            )
        if request.outro_path:
            duration = request.outro_duration_seconds or _probe_duration(
                request.outro_path,
                self.ffmpeg,
            )
            clips.append(
                VideoClip(
                    request.outro_path,
                    duration,
                    "fade",
                    is_image=False,
                    fit_mode=request.fit_mode,
                )
            )
        return clips

    def compose(self, request: VideoCompositionRequest) -> VideoCompositionResult:
        clips = self._with_intro_outro(request)
        if not clips:
            raise ValueError("视频合成至少需要一个镜头")
        if request.width < 64 or request.height < 64 or request.fps < 1:
            raise ValueError("视频画幅或帧率无效")
        if request.fit_mode not in _FIT_MODES:
            raise ValueError(f"不支持的画面适配方式：{request.fit_mode}")
        if request.source_audio_policy not in {"mute", "replace_by_voice"}:
            raise ValueError(f"不支持的源视频音轨策略：{request.source_audio_policy}")
        if request.source_audio_policy == "replace_by_voice" and request.voice_path is None:
            raise ValueError("使用主配音替换源音轨时必须提供配音")
        if request.logo_position not in _LOGO_POSITIONS:
            raise ValueError(f"不支持的 Logo 位置：{request.logo_position}")
        if not 16 <= request.logo_width <= 2048:
            raise ValueError("Logo 宽度必须在 16 到 2048 之间")
        if not 0 <= request.background_music_volume <= 2:
            raise ValueError("背景音乐音量必须在 0 到 2 之间")
        subtitle_force_style = _subtitle_force_style(request.subtitle_style)
        for clip in clips:
            if not clip.path.is_file():
                raise ValueError(f"镜头素材不存在：{clip.path.name}")
            if clip.duration_seconds <= 0:
                raise ValueError("镜头时长必须大于 0")
            if clip.transition_duration_seconds < 0:
                raise ValueError("转场时长不能小于 0")
            if clip.fit_mode not in _FIT_MODES:
                raise ValueError(f"不支持的画面适配方式：{clip.fit_mode}")
        for path in (
            request.voice_path,
            request.subtitle_path,
            request.background_music_path,
            request.logo_path,
        ):
            if path is not None and not path.is_file():
                raise ValueError(f"合成素材不存在：{path.name}")

        command = [self.ffmpeg, "-y"]
        for clip in clips:
            is_image = (
                clip.is_image
                if clip.is_image is not None
                else clip.path.suffix.lower() in _IMAGE_EXTENSIONS
            )
            if is_image:
                command.extend(
                    [
                        "-loop",
                        "1",
                        "-framerate",
                        str(request.fps),
                        "-t",
                        f"{clip.duration_seconds:.3f}",
                    ]
                )
            command.extend(["-i", str(clip.path)])

        logo_index: int | None = None
        voice_index: int | None = None
        music_index: int | None = None
        next_index = len(clips)
        if request.logo_path:
            logo_index = next_index
            next_index += 1
            command.extend(["-loop", "1", "-i", str(request.logo_path)])
        if request.voice_path:
            voice_index = next_index
            next_index += 1
            command.extend(["-i", str(request.voice_path)])
        if request.background_music_path:
            music_index = next_index
            command.extend(["-stream_loop", "-1", "-i", str(request.background_music_path)])

        filters: list[str] = []
        transitions_applied: list[str] = []
        for index, clip in enumerate(clips):
            if clip.fit_mode == "cover":
                resize_filters = (
                    f"scale={request.width}:{request.height}:"
                    "force_original_aspect_ratio=increase,"
                    f"crop={request.width}:{request.height}"
                )
            else:
                resize_filters = (
                    f"scale={request.width}:{request.height}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={request.width}:{request.height}:(ow-iw)/2:(oh-ih)/2"
                )
            clip_filters = (
                f"{resize_filters},"
                f"fps={request.fps},settb=1/{request.fps},setsar=1,"
                f"tpad=stop_mode=clone:stop_duration={clip.duration_seconds:.3f},"
                f"trim=duration={clip.duration_seconds:.3f},"
                f"setpts=N/({request.fps}*TB)"
            )
            if index > 0:
                transition = clip.transition.strip().lower()
                duration = min(
                    clip.transition_duration_seconds,
                    clips[index - 1].duration_seconds / 2,
                    clip.duration_seconds / 2,
                )
                if transition not in {"", "none", "cut"}:
                    if transition not in _TRANSITIONS:
                        raise ValueError(f"不支持的视频转场：{clip.transition}")
                    color = "white" if transition == "fadewhite" else "black"
                    clip_filters += f",fade=t=in:st=0:d={duration:.3f}:color={color}"
                    transitions_applied.append(transition)
                else:
                    transitions_applied.append("none")
            if index + 1 < len(clips):
                next_clip = clips[index + 1]
                transition = next_clip.transition.strip().lower()
                duration = min(
                    next_clip.transition_duration_seconds,
                    clip.duration_seconds / 2,
                    next_clip.duration_seconds / 2,
                )
                if transition not in {"", "none", "cut"}:
                    if transition not in _TRANSITIONS:
                        raise ValueError(f"不支持的视频转场：{next_clip.transition}")
                    color = "white" if transition == "fadewhite" else "black"
                    start = max(0.0, clip.duration_seconds - duration)
                    clip_filters += f",fade=t=out:st={start:.3f}:d={duration:.3f}:color={color}"
            filters.append(f"[{index}:v]{clip_filters}[v{index}]")
        total_duration = sum(clip.duration_seconds for clip in clips)
        if len(clips) == 1:
            current = "v0"
        else:
            current = "joined"
            inputs = "".join(f"[v{index}]" for index in range(len(clips)))
            filters.append(f"{inputs}concat=n={len(clips)}:v=1:a=0[{current}]")

        if request.subtitle_path:
            output = "with_subtitles"
            force_style = f":force_style='{subtitle_force_style}'" if subtitle_force_style else ""
            filters.append(
                f"[{current}]subtitles=filename='{_subtitle_filter_path(request.subtitle_path)}'"
                f"{force_style}"
                f"[{output}]"
            )
            current = output
        if logo_index is not None:
            output = "with_logo"
            filters.extend(
                [
                    f"[{logo_index}:v]scale={request.logo_width}:-1[logo]",
                    (
                        f"[{current}][logo]overlay={_LOGO_POSITIONS[request.logo_position]}:"
                        f"eof_action=repeat[{output}]"
                    ),
                ]
            )
            current = output

        audio_label: str | None = None
        if voice_index is not None:
            filters.append(
                f"[{voice_index}:a]aresample=48000,apad,"
                f"atrim=duration={total_duration:.3f},asetpts=PTS-STARTPTS[voice]"
            )
            audio_label = "voice"
        if music_index is not None:
            filters.append(
                f"[{music_index}:a]aresample=48000,"
                f"atrim=duration={total_duration:.3f},asetpts=PTS-STARTPTS,"
                f"volume={request.background_music_volume:.3f}[music]"
            )
            if audio_label:
                filters.append(
                    f"[{audio_label}][music]amix=inputs=2:duration=longest:"
                    f"dropout_transition=0,atrim=duration={total_duration:.3f}[mixed]"
                )
                audio_label = "mixed"
            else:
                audio_label = "music"

        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{current}]"])
        if audio_label:
            command.extend(["-map", f"[{audio_label}]", "-c:a", "aac", "-b:a", "192k"])
        else:
            command.append("-an")
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(request.fps),
                "-t",
                f"{total_duration:.3f}",
                "-movflags",
                "+faststart",
                str(request.output_path),
            ]
        )
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("FFmpeg 视频合成超时") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip().replace("\n", " ")[-1200:]
            raise RuntimeError(f"FFmpeg 视频合成失败：{stderr}") from exc
        if not request.output_path.is_file() or request.output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg 未生成有效视频文件")
        return VideoCompositionResult(
            output_path=request.output_path,
            duration_seconds=total_duration,
            width=request.width,
            height=request.height,
            clip_count=len(clips),
            transitions_applied=tuple(transitions_applied),
            subtitle_burned=request.subtitle_path is not None,
            voice_applied=request.voice_path is not None,
            background_music_applied=request.background_music_path is not None,
            logo_applied=request.logo_path is not None,
        )
