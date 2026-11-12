"""HiAgent OpenAPI adapters for the Yifabao tenant gateway.

The gateway uses Volcengine V4 signing and exposes model actions rather than
the Ark-compatible REST contract.  Credentials are read from environment
variables by the provider factory; this module never stores them in results.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import subprocess
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from PIL import Image

from .hiagent_network import configured_hiagent_base_url, hiagent_download_httpx_options
from .llm_adapters import ProviderCallError
from .providers import (
    AiVideoGenerationPort,
    AiVideoGenerationRequest,
    AiVideoTask,
    GeneratedAsset,
    ImageGenerationPort,
    ImageGenerationRequest,
    ProviderStatus,
)


class HiAgentClient:
    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        workspace_id: str,
        host: str | None = None,
        port: int | None = None,
        region: str = "cn-north-1",
        account_id: str = "1000000000",
        timeout: float = 300.0,
    ):
        if not access_key or not secret_key or not workspace_id:
            raise ValueError("HiAgent AK、SK 和工作空间不能为空")
        if not host:
            configured = urlsplit(configured_hiagent_base_url())
            host, port = configured.hostname, port or configured.port
        if not host:
            raise ValueError("HiAgent 接口地址未配置（HIAGENT_BASE_URL）")
        port = port or 443
        self.access_key = access_key
        self.secret_key = secret_key
        self.workspace_id = workspace_id
        self.host = f"{host}:{port}"
        self.gateway_host, self.gateway_port = host, port
        self.region = region
        self.account_id = account_id
        self.timeout = timeout

    @staticmethod
    def _hmac(key: bytes | str, message: str) -> bytes:
        if isinstance(key, str):
            key = key.encode()
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    def _request(
        self, action: str, body: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        try:
            # The checked-in quickstart is the single source of truth for the
            # HiAgent AK/SK gateway (endpoint :30040, service aigw, retries).
            from . import hiagent_quickstart as quickstart

            with quickstart.client_configuration(
                self.access_key,
                self.secret_key,
                self.workspace_id,
                self.gateway_host,
                self.gateway_port,
            ):
                result = quickstart.call(action, body, timeout=timeout or self.timeout)
            if isinstance(result, str) and result.strip():
                return {"task_id": result.strip()}
            return result if isinstance(result, dict) else {}
        except RuntimeError as exc:
            raise ProviderCallError(str(exc)) from exc

    def public_file_url(self, path: str) -> str:
        try:
            from . import hiagent_quickstart as quickstart

            with quickstart.client_configuration(
                self.access_key,
                self.secret_key,
                self.workspace_id,
                self.gateway_host,
                self.gateway_port,
            ):
                return str(quickstart.public_file_url(path))
        except RuntimeError as exc:
            raise ProviderCallError(str(exc)) from exc

    def list_granted_models(self) -> list[dict[str, Any]]:
        result = self._request(
            "ListModelByWorkspaceGrant",
            {
                "WorkspaceID": self.workspace_id,
                "PageNumber": 1,
                "PageSize": 100,
            },
            timeout=min(60, self.timeout),
        )
        return [
            item
            for item in result.get("Items", [])
            if isinstance(item, dict) and item.get("Granted")
        ]

    def upload_raw(self, content: bytes, *, content_type: str, filename: str) -> dict[str, Any]:
        try:
            from . import hiagent_quickstart as quickstart

            with quickstart.client_configuration(
                self.access_key,
                self.secret_key,
                self.workspace_id,
                self.gateway_host,
                self.gateway_port,
            ):
                result = quickstart.upload_raw(
                    content,
                    content_type=content_type,
                    filename=filename,
                )
            return result if isinstance(result, dict) else {}
        except RuntimeError as exc:
            raise ProviderCallError(str(exc)) from exc


def _as_size_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _pixel_dimensions(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("×", "x")
    separator = "x" if "x" in normalized else ":" if ":" in normalized else ""
    if not separator:
        return None
    left, right = normalized.split(separator, 1)
    if not left.isdigit() or not right.isdigit():
        return None
    width, height = int(left), int(right)
    if min(width, height) < 32:
        return None
    return width, height


def _parse_image_size(value: Any, ratio: Any) -> tuple[int, int]:
    size_text = _as_size_text(value)
    ratio_text = _as_size_text(ratio)
    normalized = (size_text or "").upper()
    if normalized in {"2K", "3K", "4K"}:
        ratio_presets = {
            "2K": {
                "1:1": (2048, 2048),
                "16:9": (2560, 1440),
                "9:16": (1440, 2560),
                "4:3": (2304, 1728),
                "3:4": (1728, 2304),
                "21:9": (3136, 1344),
            },
            "3K": {
                "1:1": (3072, 3072),
                "16:9": (4096, 2304),
                "9:16": (2304, 4096),
                "4:3": (3456, 2592),
                "3:4": (2592, 3456),
                "21:9": (4704, 2016),
            },
            "4K": {
                "1:1": (4096, 4096),
                "16:9": (4096, 2304),
                "9:16": (2304, 4096),
                "4:3": (4096, 3072),
                "3:4": (3072, 4096),
                "21:9": (4704, 2016),
            },
        }
        width, height = ratio_presets[normalized].get(
            ratio_text or "1:1", ratio_presets[normalized]["1:1"]
        )
    else:
        pixels = _pixel_dimensions(size_text) or _pixel_dimensions(ratio_text)
        if pixels:
            width, height = pixels
        else:
            presets = {
                "1:1": (2048, 2048),
                "16:9": (2560, 1440),
                "9:16": (1440, 2560),
                "4:3": (2304, 1728),
                "3:4": (1728, 2304),
                "21:9": (3136, 1344),
            }
            width, height = presets.get(ratio_text or "16:9", (2560, 1440))
    width, height = max(480, width // 32 * 32), max(480, height // 32 * 32)
    if width * height < 3_686_400:
        raise ValueError("Seedream 图片总像素不能低于 3686400")
    return width, height


_SEEDANCE_MIN_IMAGE_EDGE = 300
_MAX_HIAGENT_UPLOAD_BYTES = 200 * 1024 * 1024
# UploadRaw on the tenant gateway returns HTTP 413 around 6.7MB; 954KB succeeds.
_HIAGENT_UPLOAD_RAW_MAX_BYTES = 1_000_000
_HIAGENT_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}


def _uploaded_path_from_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        return None
    path = (parse_qs(parsed.query).get("Path") or [""])[0]
    return path if path.startswith("upload/") else None


def _filename_from_url(url: str, fallback: str) -> str:
    parsed = urlsplit(url)
    name = Path(unquote(parsed.path)).name
    if not name or name.lower() in {"down", "up", "download", "upload"}:
        return fallback
    return name


def _decode_data_url(url: str) -> tuple[bytes, str]:
    header, separator, payload = url.partition(",")
    if not separator or not header.lower().startswith("data:") or ";base64" not in header.lower():
        raise ValueError("参考素材 Data URL 无效")
    media_type = header[5:].split(";", 1)[0].strip().lower() or "application/octet-stream"
    return base64.b64decode(payload), media_type


def _read_file_uri(url: str) -> tuple[bytes, str]:
    parsed = urlsplit(url)
    raw = unquote(parsed.path)
    if os.name == "nt" and raw.startswith("/") and len(raw) >= 3 and raw[2] == ":":
        raw = raw[1:]
    path = Path(raw)
    if not path.is_file():
        raise ValueError("参考素材本地文件不存在")
    size = path.stat().st_size
    if size > _MAX_HIAGENT_UPLOAD_BYTES:
        raise ValueError("参考素材超过大小限制")
    media_type = _media_type_for_name(
        path.name, "image" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else "video"
    )
    return path.read_bytes(), media_type


def _media_type_for_name(name: str, kind: str) -> str:
    suffix = Path(name).suffix.lower()
    return _HIAGENT_MEDIA_TYPES.get(suffix) or ("image/png" if kind == "image" else "video/mp4")


def _filename_for_media_type(name: str, media_type: str) -> str:
    stem = Path(name).stem or "reference"
    if media_type == "image/jpeg":
        return f"{stem}.jpg"
    if media_type == "image/png":
        return f"{stem}.png"
    if media_type == "video/quicktime":
        return f"{stem}.mov"
    if media_type.startswith("video/"):
        return f"{stem}.mp4"
    return name


def _is_upload_too_large(error: BaseException) -> bool:
    text = str(error)
    return (
        "413" in text
        or "payload too large" in text.lower()
        or "request entity too large" in text.lower()
    )


def _compress_reference_video(content: bytes) -> bytes:
    from .timeline_media import _ffmpeg_executable

    try:
        ffmpeg = _ffmpeg_executable()
    except Exception as exc:
        raise ProviderCallError(
            "参考视频超过上传限制，且压缩失败。请换一段不超过 15 秒、体积更小的参考视频"
        ) from exc
    with TemporaryDirectory() as folder:
        source = Path(folder) / "source.mp4"
        output = Path(folder) / "reference.mp4"
        source.write_bytes(content)
        try:
            completed = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(source),
                    "-t",
                    "15",
                    "-vf",
                    "scale='min(720,iw)':-2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "32",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    "-movflags",
                    "+faststart",
                    str(output),
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderCallError(
                "参考视频超过上传限制，且压缩失败。请换一段不超过 15 秒、体积更小的参考视频"
            ) from exc
        if completed.returncode != 0 or not output.is_file():
            raise ProviderCallError(
                "参考视频超过上传限制，且压缩失败。请换一段不超过 15 秒、体积更小的参考视频"
            )
        compressed = output.read_bytes()
    if not compressed:
        raise ProviderCallError("参考视频压缩结果为空")
    if len(compressed) > _HIAGENT_UPLOAD_RAW_MAX_BYTES:
        raise ProviderCallError(
            "参考视频压缩后仍超过上传限制。请换一段不超过 15 秒、体积更小的参考视频"
        )
    return compressed


def _ensure_min_image_edge(content: bytes, media_type: str) -> tuple[bytes, str]:
    with Image.open(BytesIO(content)) as image:
        width, height = image.size
        min_edge = min(width, height)
        normalized_type = media_type.split(";", 1)[0].strip().lower()
        needs_convert = normalized_type not in {"image/jpeg", "image/png"}
        if min_edge >= _SEEDANCE_MIN_IMAGE_EDGE and not needs_convert:
            return content, normalized_type or "image/png"
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        if min_edge < _SEEDANCE_MIN_IMAGE_EDGE:
            scale = _SEEDANCE_MIN_IMAGE_EDGE / max(min_edge, 1)
            image = image.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.Resampling.LANCZOS,
            )
        buffer = BytesIO()
        if normalized_type == "image/jpeg" or image.mode == "RGB":
            if image.mode == "RGBA":
                image = image.convert("RGB")
            image.save(buffer, format="JPEG", quality=92)
            return buffer.getvalue(), "image/jpeg"
        image.save(buffer, format="PNG")
        return buffer.getvalue(), "image/png"


def _absolute_public_url(url: str) -> str:
    if url.startswith("https://"):
        return url
    host = urlsplit(configured_hiagent_base_url()).hostname
    if url.startswith("/") and host:
        return f"https://{host}{url}"
    return url


def _normalize_reference_image(content: bytes, media_type: str) -> tuple[bytes, str]:
    normalized = (media_type or "image/png").split(";", 1)[0].strip().lower()
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    if normalized not in {"image/jpeg", "image/png"}:
        content, normalized = _ensure_min_image_edge(content, normalized)
    if normalized not in {"image/jpeg", "image/png"}:
        raise ValueError("HiAgent 参考图片只支持 JPEG 或 PNG")
    return content, normalized


def _image_edit_file_info(
    uploaded: dict[str, Any] | None,
    *,
    filename: str,
    media_type: str,
) -> dict[str, Any]:
    public_url = str((uploaded or {}).get("Url") or "")
    if not public_url.startswith("https://"):
        public_url = _absolute_public_url(public_url)
    if not public_url.startswith("https://"):
        raise ProviderCallError("hiagent 参考图片上传未返回可访问地址")
    file_obj: dict[str, Any] = {
        "Url": public_url,
        "Name": filename,
        "ContentType": media_type,
    }
    path = str((uploaded or {}).get("Path") or "")
    if path:
        file_obj["Path"] = path
    size = (uploaded or {}).get("Size")
    if isinstance(size, int) and size > 0:
        file_obj["Size"] = size
    return file_obj


def _hiagent_video_task_id(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if not isinstance(result, dict):
        return ""
    for key in ("task_id", "TaskID", "TaskId", "ID", "Id", "id"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("Task", "task", "Data"):
        found = _hiagent_video_task_id(result.get(nested_key))
        if found:
            return found
    tasks = result.get("Tasks") or result.get("tasks")
    if isinstance(tasks, list) and tasks:
        return _hiagent_video_task_id(tasks[0])
    return ""


def prepare_video_file_input(
    url: str,
    *,
    name: str,
    kind: str,
    role: str,
    uploader: Any,
    public_url_for_path: Any = None,
) -> dict[str, Any]:
    existing_path = _uploaded_path_from_url(url)
    if existing_path:
        public_url = _absolute_public_url(url)
        if "Key=" not in public_url and callable(public_url_for_path):
            public_url = str(public_url_for_path(existing_path) or "")
        if not public_url.startswith("https://"):
            raise ProviderCallError("hiagent 参考素材缺少可访问地址")
        filename = _filename_from_url(url, name)
        media_type = _media_type_for_name(filename, kind)
        return {
            "File": {
                "Path": existing_path,
                "Name": filename,
                "Type": kind,
                "ContentType": media_type,
                "Url": public_url,
            },
            "Role": role,
        }

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme == "https":
        # Public HTTPS (including prior Seedance outputs) must not be downloaded
        # and re-uploaded: UploadRaw returns HTTP 413 around a few megabytes.
        filename = _filename_from_url(url, name)
        return {
            "File": {
                "Name": filename,
                "Type": kind,
                "ContentType": _media_type_for_name(filename, kind),
                "Url": url,
            },
            "Role": role,
        }
    if scheme == "data":
        content, media_type = _decode_data_url(url)
        filename = name
    elif scheme == "file":
        content, media_type = _read_file_uri(url)
        filename = _filename_from_url(url, name)
    else:
        raise ValueError("参考素材地址不受支持")
    if not content:
        raise ValueError("参考素材为空")
    if len(content) > _MAX_HIAGENT_UPLOAD_BYTES:
        raise ValueError("参考素材超过大小限制")
    if kind == "image":
        content, media_type = _ensure_min_image_edge(content, media_type)
        filename = _filename_for_media_type(filename, media_type)
    elif kind == "video" and len(content) > _HIAGENT_UPLOAD_RAW_MAX_BYTES:
        content = _compress_reference_video(content)
        media_type = "video/mp4"
        filename = _filename_for_media_type(filename, media_type)
    try:
        uploaded = uploader(content, content_type=media_type, filename=filename)
    except ProviderCallError as exc:
        if kind != "video" or not _is_upload_too_large(exc):
            raise
        content = _compress_reference_video(content)
        media_type = "video/mp4"
        filename = _filename_for_media_type(filename, media_type)
        uploaded = uploader(content, content_type=media_type, filename=filename)
    path = str((uploaded or {}).get("Path") or "")
    if not path:
        raise ProviderCallError("hiagent 文件上传未返回路径")
    file_obj: dict[str, Any] = {
        "Path": path,
        "Name": filename,
        "Type": kind,
        "ContentType": media_type,
    }
    size = (uploaded or {}).get("Size")
    if isinstance(size, int) and size > 0:
        file_obj["Size"] = size
    public_url = str((uploaded or {}).get("Url") or "")
    if not public_url.startswith("https://"):
        public_url = _absolute_public_url(public_url)
    if not public_url.startswith("https://") and callable(public_url_for_path):
        public_url = str(public_url_for_path(path) or "")
    if not public_url.startswith("https://"):
        raise ProviderCallError("hiagent 文件上传未返回可访问地址")
    file_obj["Url"] = public_url
    return {"File": file_obj, "Role": role}


class HiAgentImageGenerationPort(ImageGenerationPort):
    def __init__(
        self,
        *,
        client: HiAgentClient,
        model_service_id: str,
        provider_id: str = "hiagent-image",
        max_candidates: int = 1,
        supported_sizes: tuple[str, ...] = (),
        supported_ratios: tuple[str, ...] = (),
    ):
        self.client = client
        self.model_service_id = model_service_id
        self.max_candidates = max_candidates
        self.status = ProviderStatus(
            provider_id, "hiagent-seedream", "image-generation", "api", True, True, model_service_id
        )
        self.capabilities = {
            "count": {"min": 1, "max": max_candidates},
            "sizes": list(supported_sizes or ("2K", "3K")),
            "ratios": list(supported_ratios or ("1:1", "16:9", "9:16", "4:3", "3:4", "21:9")),
            "custom_size": True,
            "watermark": True,
            "reference_images": {"max": 10},
            "parameter_source": "hiagent-model-capability",
        }

    def generate(self, request: ImageGenerationRequest) -> list[GeneratedAsset]:
        if not request.prompt.strip():
            raise ValueError("图片提示词不能为空")
        if not 1 <= request.count <= self.max_candidates:
            raise ValueError(f"单次图片候选数量必须在 1 到 {self.max_candidates} 之间")
        width, height = _parse_image_size(request.size, request.ratio)
        body = {
            "WorkspaceID": self.client.workspace_id,
            "ModelServiceID": self.model_service_id,
            "Prompt": request.prompt,
            "Size": {"Width": width, "Height": height},
            "MaxImages": 1,
            "Watermark": bool(request.watermark),
            "WebSearch": False,
        }
        action = "ImageGen"
        if request.reference_images:
            if len(request.reference_images) > 10:
                raise ValueError("HiAgent 参考图片最多支持 10 张")
            files = []
            for index, content in enumerate(request.reference_images):
                media_type = (
                    request.reference_media_types[index]
                    if index < len(request.reference_media_types)
                    else "image/png"
                )
                content, media_type = _normalize_reference_image(content, media_type)
                extension = "jpg" if media_type == "image/jpeg" else "png"
                filename = f"reference-{index + 1}.{extension}"
                # Data URLs in ImageFiles crash the HiAgent gateway with an
                # empty HTTP 500, same as video Files. Upload first, then pass
                # Path plus a fetchable HTTPS URL like the experience-center UI.
                uploaded = self.client.upload_raw(
                    content,
                    content_type=media_type,
                    filename=filename,
                )
                files.append(
                    _image_edit_file_info(
                        uploaded if isinstance(uploaded, dict) else None,
                        filename=filename,
                        media_type=media_type,
                    )
                )
            action = "CreateImageEdit"
            body["ImageFiles"] = files
        assets: list[GeneratedAsset] = []
        # HiAgent's MaxImages is an upper bound, not a guaranteed result count.
        # In particular Seedream commonly returns one image for MaxImages > 1.
        # Request each candidate explicitly, reusing uploaded reference files.
        # A rejected/empty request still fails the operation: never duplicate an
        # earlier image or silently treat a partial batch as the requested batch.
        for index in range(request.count):
            result = self.client._request(action, dict(body))
            raw_items = result.get("Data")
            item = raw_items[0] if isinstance(raw_items, list) and raw_items else None
            url = item.get("Url") if isinstance(item, dict) else None
            if not url:
                raise ProviderCallError("hiagent 图片接口未返回图片")
            try:
                response = httpx.get(url, timeout=300, **hiagent_download_httpx_options(url))
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProviderCallError("hiagent 图片下载失败") from exc
            content = response.content
            extension = "jpg" if content[:3] == b"\xff\xd8\xff" else "png"
            assets.append(
                GeneratedAsset(
                    content,
                    "image/jpeg" if extension == "jpg" else "image/png",
                    extension,
                    self.status.provider_id,
                    self.model_service_id,
                    {
                        "width": width,
                        "height": height,
                        "ratio": f"{width}:{height}",
                        "candidate_index": index,
                    },
                )
            )
        return assets


class HiAgentVideoGenerationPort(AiVideoGenerationPort):
    def __init__(
        self,
        *,
        client: HiAgentClient,
        pro_model: str,
        fast_model: str,
        provider_id: str = "hiagent-video",
        durations: tuple[int, ...] = tuple(range(5, 16)),
        ratios: tuple[str, ...] = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"),
        resolutions: tuple[str, ...] = (),
    ):
        self.client = client
        self.pro_model, self.fast_model = pro_model, fast_model
        self.durations, self.ratios = durations, ratios
        self._active_models: set[str] = set()
        try:
            self._active_models = {
                str(x.get("ID") or x.get("ModelServiceID") or x.get("ModelId"))
                for x in client.list_granted_models()
                if x.get("ID") or x.get("ModelServiceID") or x.get("ModelId")
            }
        except ProviderCallError:
            # A later create call still gives the authoritative permission error.
            self._active_models = set()
        self.pro_available = pro_model in self._active_models
        selected_model = pro_model if self.pro_available else fast_model
        self.status = ProviderStatus(
            provider_id,
            "hiagent-seedance",
            "ai-video-generation",
            "async-api",
            True,
            True,
            selected_model,
        )
        self.capabilities = {
            "async": True,
            "text_to_video": True,
            "image_to_video": True,
            "reference_images": {"max": 10, "provider_native_reference_supported": True},
            "reference_videos": {
                "max": 1,
                "total_duration_seconds": 15,
                "provider_native_reference_supported": True,
                "local_file_upload": True,
                "transports": ["hiagent-runtime-upload", "https-url"],
            },
            "durations": list(durations),
            "ratios": list(ratios),
            "total_duration_seconds": 15,
            "resolutions": [
                value
                for value in (resolutions or ("480p", "720p", "1080p"))
                if value != "1080p" or self.pro_available
            ],
            "generate_audio": True,
            "parameter_source": "hiagent-model-capability",
        }

    def _choose_model(self, resolution: str | None) -> str:
        # Pro first when it is granted. Fast is the fallback when Pro is not
        # granted, when create fails, or when the Pro task later fails.
        if self.pro_model in self._active_models:
            return self.pro_model
        return self.fast_model

    def submit(self, request: AiVideoGenerationRequest) -> AiVideoTask:
        if not request.prompt.strip():
            raise ValueError("视频提示词不能为空")
        duration = int(request.duration_seconds or 5)
        if duration not in self.durations:
            raise ValueError(f"视频时长必须为 {self.durations[0]}–{self.durations[-1]} 秒")
        ratio = request.ratio or "adaptive"
        if ratio not in self.ratios:
            raise ValueError(f"视频比例不支持：{ratio}")
        resolution = request.resolution or "720p"
        requested_resolution = resolution
        model = self._choose_model(resolution)
        if resolution == "1080p" and model == self.fast_model and model != self.pro_model:
            # Keep the request usable when Pro is not granted.  The effective
            # resolution is recorded in task metadata so the UI can explain
            # the automatic downgrade.
            resolution = "720p"
        payload = {
            "Prompt": request.prompt,
            "Model": model,
            "Implement": "volcengine",
            "Ratio": ratio,
            "Resolution": resolution,
            "Duration": duration,
            "GenerateAudio": bool(request.generate_audio),
            "Watermark": False,
            "WorkspaceID": self.client.workspace_id,
        }
        # The experience-center UI uploads to HiAgent UP first, then creates
        # the task with nested File.Path objects. Data URLs in Files crash the
        # gateway with an empty HTTP 500.
        image_urls = tuple(url for url in (request.image_url, *request.reference_image_urls) if url)
        video_urls = tuple(url for url in request.reference_video_urls if url)
        files = []
        # Reference-to-video: images + prompt, video + prompt, or both.
        # Mixing first_frame with extra reference images is rejected as invalid
        # parameters. Single image without a video stays first-frame i2v.
        image_role = "reference_image" if video_urls or len(image_urls) > 1 else "first_frame"
        for index, url in enumerate(image_urls):
            files.append(
                prepare_video_file_input(
                    url,
                    name=f"reference_image_{index + 1}.png",
                    kind="image",
                    role=image_role,
                    uploader=self.client.upload_raw,
                    public_url_for_path=getattr(self.client, "public_file_url", None),
                )
            )
        for index, url in enumerate(video_urls):
            files.append(
                prepare_video_file_input(
                    url,
                    name=f"reference_video_{index + 1}.mp4",
                    kind="video",
                    role="reference_video",
                    uploader=self.client.upload_raw,
                    public_url_for_path=getattr(self.client, "public_file_url", None),
                )
            )
        if files:
            payload["Files"] = files
        try:
            result = self.client._request("CreateVideoGenTask", payload)
        except ProviderCallError:
            # A granted Pro can still be temporarily unavailable. Retry with
            # fast only when the first call did not return a task ID.
            if model != self.fast_model:
                model = self.fast_model
                if resolution == "1080p":
                    resolution = "720p"
                payload.update({"Model": model, "Resolution": resolution})
                result = self.client._request("CreateVideoGenTask", payload)
            else:
                raise
        task_id = _hiagent_video_task_id(result)
        if not task_id:
            raise ProviderCallError("hiagent 视频接口未返回任务 ID")
        return AiVideoTask(
            task_id,
            "queued",
            self.status.provider_id,
            model,
            metadata={
                "ratio": ratio,
                "resolution": resolution,
                "requested_resolution": requested_resolution,
                "downgraded": requested_resolution != resolution,
                "duration": duration,
                "retry_payload": dict(payload),
            },
        )

    def submit_fast_retry(self, retry_payload: dict[str, Any]) -> AiVideoTask:
        if not isinstance(retry_payload, dict) or not retry_payload.get("Prompt"):
            raise ProviderCallError("没有可供 Fast 重试的视频请求")
        if str(retry_payload.get("Model") or "") == self.fast_model:
            raise ProviderCallError("当前已经是 Fast 模型，不再重复提交")
        payload = dict(retry_payload)
        payload["Model"] = self.fast_model
        if str(payload.get("Resolution") or "") == "1080p":
            payload["Resolution"] = "720p"
        result = self.client._request("CreateVideoGenTask", payload)
        task_id = _hiagent_video_task_id(result)
        if not task_id:
            raise ProviderCallError("hiagent 视频接口未返回任务 ID")
        return AiVideoTask(
            task_id,
            "queued",
            self.status.provider_id,
            self.fast_model,
            metadata={
                "ratio": payload.get("Ratio"),
                "resolution": payload.get("Resolution"),
                "requested_resolution": retry_payload.get("Resolution"),
                "downgraded": str(retry_payload.get("Resolution") or "") == "1080p",
                "duration": payload.get("Duration"),
                "retry_payload": dict(payload),
            },
        )

    def get(self, provider_task_id: str) -> AiVideoTask:
        result = self.client._request(
            "GetVideoGenTask",
            {"WorkspaceID": self.client.workspace_id, "TaskID": provider_task_id},
            timeout=60,
        )
        tasks = [item for item in (result.get("Tasks") or []) if isinstance(item, dict)]
        task = next(
            (
                item
                for item in tasks
                if str(
                    item.get("TaskID")
                    or item.get("task_id")
                    or item.get("ID")
                    or item.get("Id")
                    or item.get("id")
                    or ""
                )
                == provider_task_id
            ),
            None,
        )
        if task is None:
            raise ProviderCallError("hiagent 视频任务不存在或尚未返回")
        status_map = {
            "queued": "queued",
            "running": "running",
            "succeeded": "succeeded",
            "success": "succeeded",
            "failed": "failed",
            "error": "failed",
            "canceled": "cancelled",
            "cancelled": "cancelled",
        }
        status = status_map.get(str(task.get("Status", "queued")).lower(), "failed")
        data = task.get("Data") or {}
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        if not isinstance(data, dict):
            data = {}
        error = task.get("Error")
        error_code = None
        if isinstance(error, dict):
            error_message = (
                str(
                    error.get("Message")
                    or error.get("message")
                    or error.get("Code")
                    or error.get("code")
                    or ""
                )
                or None
            )
            raw_code = error.get("Code") or error.get("code")
            if isinstance(raw_code, str) and raw_code.strip():
                error_code = raw_code.strip()
        else:
            error_message = str(error) if error else None
        return AiVideoTask(
            provider_task_id,
            status,
            self.status.provider_id,
            str(task.get("Model") or self.status.model),
            video_url=data.get("Url"),
            error_code=error_code,
            error_message=error_message,
            metadata={"usage": task.get("Usage", {}), "model_name": task.get("ModelName")},
        )

    def cancel(self, provider_task_id: str) -> AiVideoTask:
        raise ValueError("HiAgent 视频任务暂不支持取消接口")
