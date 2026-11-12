"""Generate private chat artifacts through the configured content media adapters.

Callers must resolve model allow-lists and reference ownership before calling.
This boundary also confines all filesystem access to the private chat tree.
Image subclasses retain the existing provider payloads and add bounded result
downloads; no process-wide HTTP monkeypatching or user supplied URLs are used.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError

from .ai_model_config import model_settings
from .config import Settings
from .generation_errors import safe_generation_error_text
from .hiagent_adapters import (
    HiAgentImageGenerationPort,
    HiAgentVideoGenerationPort,
    _image_edit_file_info,
    _normalize_reference_image,
    _parse_image_size,
)
from .hiagent_network import (
    hiagent_download_httpx_options,
    hiagent_result_download_prefixes,
    safe_result_download_url,
)
from .llm_adapters import ProviderCallError
from .media_adapters import (
    LocalAssetStore,
    VolcengineArkAiVideoGenerationPort,
    VolcengineArkImageGenerationPort,
    _decode_base64,
    _safe_http_error,
    download_generated_video,
    wait_for_ai_video_task,
)
from .media_ingest import MediaValidationError, probe_media, validate_upload_header
from .openai_image_adapter import OpenAICompatibleImageGenerationPort
from .provider_dependencies import hiagent_client
from .providers import AiVideoGenerationRequest, GeneratedAsset, ImageGenerationRequest

MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_WAIT_SECONDS = 900
_IMAGE_TYPES = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}


class ChatMediaError(ValueError):
    """Safe, locally authored message suitable for the chat error response."""


def _private_path(settings: Settings, path: Path) -> Path:
    root = (settings.data_dir / "private" / "chat").resolve()
    resolved = Path(path).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ChatMediaError("AI 对话媒体文件必须位于私有对话目录")
    return resolved


def _image_type(content: bytes) -> tuple[str, str]:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ChatMediaError("图片不能为空或超过 30 MB")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in _IMAGE_TYPES or image.width * image.height > 20_000_000:
                raise ChatMediaError("图片需要是 JPG、PNG 或 WebP，且不能超过 2000 万像素")
            result = _IMAGE_TYPES[image.format]
            image.verify()
            return result
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        raise ChatMediaError("图片无法解码，请重新上传有效的 JPG、PNG 或 WebP 图片") from None


def _reference(settings: Settings, path: Path | None) -> tuple[bytes, str] | None:
    if path is None:
        return None
    source = _private_path(settings, path)
    if not source.is_file() or source.stat().st_size > MAX_IMAGE_BYTES:
        raise ChatMediaError("参考图片不存在或超过 30 MB，请重新上传")
    with source.open("rb") as stream:
        content = stream.read(MAX_IMAGE_BYTES + 1)
    media_type, _ = _image_type(content)
    return content, media_type


def _safe_image_url(url: str, *, trusted_download_prefixes: tuple[str, ...] = ()) -> str:
    return safe_result_download_url(url, trusted_prefixes=trusted_download_prefixes)


def _download_image(
    url: str, *, timeout_seconds: float, trusted_download_prefixes: tuple[str, ...] = ()
) -> bytes:
    url = _safe_image_url(url, trusted_download_prefixes=trusted_download_prefixes)
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
            if 300 <= response.status_code < 400:
                raise ProviderCallError("图片下载地址发生跳转，请重新生成")
            media_type = response.headers.get("content-type", "application/octet-stream")
            media_type = media_type.split(";", 1)[0].strip().lower()
            if media_type not in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "application/octet-stream",
            }:
                raise ProviderCallError("图片下载返回了不支持的文件类型")
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    size = int(declared)
                except ValueError:
                    raise ProviderCallError("图片下载返回无效的文件大小") from None
                if not 0 < size <= MAX_IMAGE_BYTES:
                    raise ProviderCallError("图片下载为空或超过 30 MB")
            content = bytearray()
            for chunk in response.iter_bytes(1024 * 1024):
                content.extend(chunk)
                if len(content) > MAX_IMAGE_BYTES:
                    raise ProviderCallError("图片下载超过 30 MB")
            return bytes(content)
    except httpx.HTTPError as exc:
        raise _safe_http_error("chat-image-download", exc) from exc


def _asset(content: bytes, provider_id: str, model: str) -> GeneratedAsset:
    try:
        mime_type, extension = _image_type(content)
    except ValueError as exc:
        raise ProviderCallError("图片接口返回的图片文件无效，请重新生成") from exc
    return GeneratedAsset(content, mime_type, extension, provider_id, model)


class _ChatArkImagePort(VolcengineArkImageGenerationPort):
    """The existing Ark image contract with checked and bounded URL downloads."""

    def generate(self, request: ImageGenerationRequest) -> list[GeneratedAsset]:
        if request.count != 1:
            raise ChatMediaError("AI 对话每次生成一张图片")
        payload = {
            "model": self.model,
            "prompt": request.prompt,
            "response_format": "b64_json",
            "watermark": request.watermark,
        }
        size = self._effective_size(request.size, request.ratio)
        if size:
            payload["size"] = size
        if self._modern_seedream:
            payload["sequential_image_generation"] = "disabled"
        if request.reference_images:
            payload["image"] = [
                "data:" + media_type + ";base64," + base64.b64encode(content).decode("ascii")
                for content, media_type in zip(
                    request.reference_images, request.reference_media_types, strict=True
                )
            ]
        try:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
            item = result["data"][0]
            if isinstance(item.get("b64_json"), str):
                if len(item["b64_json"]) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                    raise ProviderCallError("图片接口返回的图片超过 30 MB")
                content = _decode_base64(item["b64_json"], self.status.provider_id)
            elif isinstance(item.get("url"), str):
                content = _download_image(item["url"], timeout_seconds=self.timeout_seconds)
            else:
                raise ProviderCallError("图片接口未返回图片，请重新生成")
        except httpx.HTTPError as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ProviderCallError("图片接口返回的结果无效，请重新生成") from exc
        return [_asset(content, self.status.provider_id, self.model)]


class _ChatHiAgentImagePort(HiAgentImageGenerationPort):
    """Reuse HiAgent upload/edit contracts and replace its unbounded download."""

    def __init__(self, *, trusted_download_prefixes: tuple[str, ...] = (), **kwargs):
        super().__init__(**kwargs)
        self.trusted_download_prefixes = trusted_download_prefixes

    def generate(self, request: ImageGenerationRequest) -> list[GeneratedAsset]:
        if request.count != 1:
            raise ChatMediaError("AI 对话每次生成一张图片")
        width, height = _parse_image_size(request.size, request.ratio)
        payload = {
            "WorkspaceID": self.client.workspace_id,
            "ModelServiceID": self.model_service_id,
            "Prompt": request.prompt,
            "Size": {"Width": width, "Height": height},
            "MaxImages": 1,
            "Watermark": bool(request.watermark),
            "WebSearch": False,
        }
        if request.reference_images:
            files = []
            for index, (content, mime_type) in enumerate(
                zip(request.reference_images, request.reference_media_types, strict=True)
            ):
                content, mime_type = _normalize_reference_image(content, mime_type)
                name = f"reference-{index + 1}.{'jpg' if mime_type == 'image/jpeg' else 'png'}"
                uploaded = self.client.upload_raw(content, content_type=mime_type, filename=name)
                files.append(_image_edit_file_info(uploaded, filename=name, media_type=mime_type))
            payload["ImageFiles"] = files
        result = self.client._request(
            "CreateImageEdit" if request.reference_images else "ImageGen", payload
        )
        try:
            url = result["Data"][0]["Url"]
        except (KeyError, IndexError, TypeError):
            raise ProviderCallError("图片接口未返回图片，请重新生成") from None
        content = _download_image(
            url,
            timeout_seconds=self.client.timeout,
            trusted_download_prefixes=self.trusted_download_prefixes,
        )
        return [_asset(content, self.status.provider_id, self.model_service_id)]


def _image_provider(settings: Settings):
    if settings.image_provider == "hiagent":
        if not (
            settings.image_api_key and settings.hiagent_secret_key and settings.hiagent_workspace_id
        ):
            raise ChatMediaError("图片接口配置不完整，请联系管理员")
        return _ChatHiAgentImagePort(
            client=hiagent_client(settings, settings.image_api_key, settings.image_timeout_seconds),
            trusted_download_prefixes=hiagent_result_download_prefixes(settings.hiagent_base_url),
            model_service_id=settings.image_model,
            provider_id=settings.image_provider_id,
            max_candidates=settings.image_max_candidates,
            supported_sizes=settings.image_supported_sizes,
            supported_ratios=settings.image_supported_ratios,
        )
    if settings.image_provider not in {"volcengine_ark", "openai_compatible"}:
        raise ChatMediaError("当前图片接口不支持 AI 对话生成或参考图编辑，请联系管理员")
    if not settings.image_base_url or not settings.image_api_key:
        raise ChatMediaError("图片接口配置不完整，请联系管理员")
    provider_class = (
        OpenAICompatibleImageGenerationPort
        if settings.image_provider == "openai_compatible"
        else _ChatArkImagePort
    )
    return provider_class(
        provider_id=settings.image_provider_id,
        base_url=settings.image_base_url,
        api_key=settings.image_api_key,
        model=settings.image_model,
        timeout_seconds=settings.image_timeout_seconds,
        max_candidates=settings.image_max_candidates,
        supported_sizes=settings.image_supported_sizes,
        supported_ratios=settings.image_supported_ratios,
    )


def _video_provider(settings: Settings):
    if settings.ai_video_provider == "hiagent":
        if not (
            settings.ai_video_api_key
            and settings.hiagent_secret_key
            and settings.hiagent_workspace_id
        ):
            raise ChatMediaError("视频接口配置不完整，请联系管理员")
        # Both slots deliberately point to the selected model. User selection
        # must never silently produce a video using another catalog model.
        return HiAgentVideoGenerationPort(
            client=hiagent_client(
                settings, settings.ai_video_api_key, settings.ai_video_timeout_seconds
            ),
            pro_model=settings.ai_video_model,
            fast_model=settings.ai_video_model,
            provider_id=settings.ai_video_provider_id,
            durations=settings.ai_video_supported_durations,
            ratios=settings.ai_video_supported_ratios,
            resolutions=settings.ai_video_supported_resolutions,
        )
    if settings.ai_video_provider != "volcengine_ark":
        raise ChatMediaError("当前视频接口不支持 AI 对话视频生成，请联系管理员")
    if not settings.ai_video_base_url or not settings.ai_video_api_key:
        raise ChatMediaError("视频接口配置不完整，请联系管理员")
    return VolcengineArkAiVideoGenerationPort(
        provider_id=settings.ai_video_provider_id,
        base_url=settings.ai_video_base_url,
        api_key=settings.ai_video_api_key,
        model=settings.ai_video_model,
        timeout_seconds=settings.ai_video_timeout_seconds,
        supported_durations=settings.ai_video_supported_durations,
        supported_ratios=settings.ai_video_supported_ratios,
        supported_resolutions=settings.ai_video_supported_resolutions,
    )


def _preferred(values, preferred):
    return preferred if preferred in values else next(iter(values), None)


def _video_audio_requested(prompt: str) -> bool:
    # Only the latest instruction controls audio; older complaints about a
    # silent result must not turn sound off again.
    current = prompt.rsplit("本轮用户制作要求（优先遵循）：", 1)[-1]
    current = re.sub(r"不要静音|不要无声|不能静音", "需要声音", current)
    return not bool(
        re.search(
            r"静音|(?:不要|不需要|无需|不用|关闭|去掉|去除|移除)(?:任何)?(?:声音|音频|音轨)|无声(?:的)?(?:视频|版本)|silent video|without (?:sound|audio)",
            current,
            re.I,
        )
    )


def _video_audio_metadata(path, requested):
    metadata = {"audio_requested": requested}
    try:
        probe = probe_media(path, kind="video", media_type="video/mp4", timeout_seconds=15)
        metadata.update(has_audio=bool(probe.audio_codec), duration_seconds=probe.duration_seconds)
    except (MediaValidationError, OSError, RuntimeError, subprocess.SubprocessError):
        # Preserve a downloadable provider result if the local probe cannot
        # inspect it, but never claim that audio has been verified.
        metadata["has_audio"] = None
    return metadata


def generate_chat_media(
    settings: Settings,
    model: dict,
    *,
    prompt: str,
    output_dir: Path,
    reference_path: Path | None = None,
) -> dict:
    """Blocking worker operation; return a service-owned image/video artifact."""
    from dataclasses import replace

    kind = model.get("kind")
    model_id = model.get("model")
    if kind not in {"image", "video"} or model.get("provider_source") != kind:
        raise ChatMediaError("请选择已开放的图片或视频生成模型")
    if (
        not isinstance(model_id, str)
        or not model_id.strip()
        or len(model_id) > 200
        or re.search(r"[\x00-\x20\x7f]", model_id)
    ):
        raise ChatMediaError("生成模型标识无效，请联系管理员")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 60000:
        raise ChatMediaError("请提供不超过 6 万字的生成要求")
    if reference_path is not None and kind == "image" and not model.get("supports_image_edit"):
        raise ChatMediaError("所选模型未开放参考图编辑，请切换支持编辑的图片模型")
    settings = model_settings(settings, kind)
    settings = replace(
        settings, **{("image_model" if kind == "image" else "ai_video_model"): model_id}
    )
    directory = _private_path(settings, output_dir)
    reference = _reference(settings, reference_path)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not directory.is_dir():
        raise ChatMediaError("AI 对话媒体输出目录不可用")
    destination = None
    metadata = {}
    try:
        if kind == "image":
            provider = _image_provider(settings)
            defaults = provider.capabilities.get("defaults", {})
            generated = provider.generate(
                ImageGenerationRequest(
                    prompt=prompt.strip(),
                    count=1,
                    size=defaults.get("size"),
                    ratio=defaults.get("ratio"),
                    reference_images=(reference[0],) if reference else (),
                    reference_media_types=(reference[1],) if reference else (),
                )
            )
            if not generated:
                raise ProviderCallError("图片接口未返回图片，请重新生成")
            artifact = generated[0]
            mime_type, extension = _image_type(artifact.content)
            destination = directory / f"{uuid4().hex}.{extension}"
            with destination.open("xb") as stream:
                if os.name != "nt":
                    os.chmod(destination, 0o600)
                stream.write(artifact.content)
            size = len(artifact.content)
            checksum = hashlib.sha256(artifact.content).hexdigest()
        else:
            provider = _video_provider(settings)
            reference_urls = (
                (
                    (
                        "data:"
                        + reference[1]
                        + ";base64,"
                        + base64.b64encode(reference[0]).decode("ascii")
                    ),
                )
                if reference
                else ()
            )
            task = provider.submit(
                AiVideoGenerationRequest(
                    prompt=prompt.strip(),
                    reference_image_urls=reference_urls,
                    duration_seconds=_preferred(settings.ai_video_supported_durations, 5),
                    ratio=_preferred(settings.ai_video_supported_ratios, "16:9"),
                    resolution=_preferred(settings.ai_video_supported_resolutions, "720p"),
                    generate_audio=_video_audio_requested(prompt),
                    extra={"generate_audio": _video_audio_requested(prompt)},
                    idempotency_key=uuid4().hex,
                )
            )
            if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,256}", task.provider_task_id):
                raise ProviderCallError("视频接口返回了无效的任务标识，请重新生成")
            if task.status in {"queued", "running"}:
                max_wait = min(settings.ai_video_max_wait_seconds, MAX_VIDEO_WAIT_SECONDS)
                task = wait_for_ai_video_task(
                    provider,
                    task.provider_task_id,
                    poll_interval_seconds=min(settings.ai_video_poll_interval_seconds, max_wait),
                    max_wait_seconds=max_wait,
                )
            if task.status != "succeeded":
                detail = safe_generation_error_text(task.error_message)
                raise ProviderCallError("视频生成失败，请重试" + (f"：{detail}" if detail else ""))
            store = LocalAssetStore(directory)
            name = f"{uuid4().hex}.mp4"
            destination = store.path_for(name)
            result = download_generated_video(
                task,
                asset_store=store,
                storage_key=name,
                timeout_seconds=min(settings.ai_video_timeout_seconds, 120),
                max_bytes=MAX_VIDEO_BYTES,
                trusted_download_prefixes=(
                    hiagent_result_download_prefixes(settings.hiagent_base_url)
                    if settings.ai_video_provider == "hiagent"
                    else ()
                ),
            )
            with destination.open("rb") as stream:
                head = stream.read(128)
            _, mime_type = validate_upload_header(
                filename=name,
                declared_media_type=result.media_type,
                head=head,
                size_bytes=result.size,
            )
            if os.name != "nt":
                os.chmod(destination, 0o600)
            size, checksum = result.size, result.checksum_sha256
            metadata = _video_audio_metadata(destination, _video_audio_requested(prompt))
        return {
            "path": destination,
            "name": destination.name,
            "mime_type": mime_type,
            "size": size,
            "kind": kind,
            "sha256": checksum,
            "metadata": metadata,
        }
    except TimeoutError:
        if destination:
            destination.unlink(missing_ok=True)
        raise ProviderCallError(
            "视频生成等待超时，请稍后重试", code="timeout", retryable=True
        ) from None
    except (OSError, MediaValidationError):
        if destination:
            destination.unlink(missing_ok=True)
        raise ProviderCallError("生成文件无法保存或格式无效，请重新生成") from None
    except Exception:
        if destination:
            destination.unlink(missing_ok=True)
        raise
