"""GPT Image generations and reference-image edits through an OpenAI-compatible API."""

from __future__ import annotations

import base64
import binascii
import io
from collections.abc import Sequence
from typing import Any

import httpx
from PIL import Image

from .hiagent_network import safe_result_download_url
from .llm_adapters import ProviderCallError
from .media_adapters import _provider_endpoint, _safe_http_error
from .providers import GeneratedAsset, ImageGenerationRequest, ProviderStatus

_RATIO_SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}
_FORMATS = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}
_MAX_IMAGE_BYTES = 30 * 1024 * 1024
_MAX_REFERENCES = 9


class OpenAICompatibleImageGenerationPort:
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
        if max_candidates < 1:
            raise ValueError("图片最大候选数必须大于 0")
        self.endpoint = _provider_endpoint(base_url, "/images/generations")
        self.edit_endpoint = self.endpoint.removesuffix("/generations") + "/edits"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_candidates = min(max_candidates, 10)
        self.sizes = supported_sizes or ("auto", *_RATIO_SIZES.values())
        self.ratios = supported_ratios or ("adaptive", *_RATIO_SIZES)
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="openai-compatible-image",
            capability="image-generation",
            mode="api",
            external=True,
            enabled=True,
            model=model,
        )
        self.capabilities = {
            "count": {"min": 1, "max": self.max_candidates},
            "sizes": list(self.sizes),
            "ratios": list(self.ratios),
            "custom_size": False,
            "custom_ratio": False,
            "defaults": {
                "size": "auto" if "auto" in self.sizes else self.sizes[0],
                "ratio": "adaptive",
            },
            "parameter_source": "provider-contract",
            "reference_images": {
                "max": _MAX_REFERENCES,
                "formats": ["image/png", "image/jpeg", "image/webp"],
                "max_bytes_per_file": _MAX_IMAGE_BYTES,
            },
            "watermark": False,
            "seed": False,
        }

    @staticmethod
    def _image_info(content: bytes) -> tuple[str, str, tuple[int, int]]:
        if not content or len(content) > _MAX_IMAGE_BYTES:
            raise ValueError("图片为空或超过 30MB")
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in _FORMATS:
                raise ValueError("图片必须是 JPEG、PNG 或 WebP")
            media_type, extension = _FORMATS[image.format]
            dimensions = image.size
            image.verify()
        return media_type, extension, dimensions

    def _size(self, request: ImageGenerationRequest) -> str:
        size = request.size or self.capabilities["defaults"]["size"]
        ratio = request.ratio or "adaptive"
        if size not in self.sizes:
            raise ValueError("请选择当前图片模型支持的尺寸")
        if ratio != "adaptive":
            if ratio not in self.ratios or ratio not in _RATIO_SIZES:
                raise ValueError("请选择当前图片模型支持的比例")
            mapped = _RATIO_SIZES[ratio]
            if size == "auto":
                size = mapped
            elif size != mapped:
                raise ValueError("图片尺寸与所选比例不一致")
            if size not in self.sizes:
                raise ValueError("当前模型未配置该比例对应的尺寸")
        return size

    def generate(self, request: ImageGenerationRequest) -> Sequence[GeneratedAsset]:
        if not request.prompt.strip():
            raise ValueError("图片提示词不能为空")
        if not 1 <= request.count <= self.max_candidates:
            raise ValueError(f"单次图片候选数量必须在 1 到 {self.max_candidates} 之间")
        if request.watermark or request.seed is not None:
            raise ValueError("当前图片模型不支持指定水印或随机种子")
        if request.extra:
            raise ValueError("当前图片模型不支持这些额外参数")
        if len(request.reference_images) > _MAX_REFERENCES:
            raise ValueError(f"单次图片生成最多允许 {_MAX_REFERENCES} 张参考图片")
        if request.reference_media_types and len(request.reference_media_types) != len(
            request.reference_images
        ):
            raise ValueError("参考图片与媒体类型数量不一致")
        files = []
        for index, content in enumerate(request.reference_images):
            try:
                media_type, extension, _ = self._image_info(content)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise ValueError("参考图片必须是有效的 JPEG、PNG 或 WebP，且不超过 30MB") from exc
            if (
                request.reference_media_types
                and request.reference_media_types[index].split(";")[0].strip().lower() != media_type
            ):
                raise ValueError("参考图片与媒体类型不一致")
            files.append(("image[]", (f"reference-{index}.{extension}", content, media_type)))
        size = self._size(request)
        # GPT Image returns base64 by default. Ark's watermark/seed/image and
        # DALL-E's response_format are not part of this contract.
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "n": request.count,
            "size": size,
        }
        try:
            with httpx.Client(
                timeout=self.timeout_seconds, trust_env=False, follow_redirects=False
            ) as client:
                headers = {"Authorization": f"Bearer {self.api_key}"}
                if files:
                    response = client.post(
                        self.edit_endpoint,
                        headers=headers,
                        data={key: str(value) for key, value in payload.items()},
                        files=files,
                    )
                else:
                    response = client.post(self.endpoint, headers=headers, json=payload)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ValueError("缺少图片结果")
                if len(result["data"]) != request.count:
                    raise ValueError("返回图片数量不符")
                assets = []
                for index, item in enumerate(result["data"]):
                    if not isinstance(item, dict):
                        raise ValueError("图片结果无效")
                    if isinstance(item.get("b64_json"), str):
                        if len(item["b64_json"]) > 4 * ((_MAX_IMAGE_BYTES + 2) // 3):
                            raise ValueError("图片过大")
                        content = base64.b64decode(item["b64_json"], validate=True)
                    elif isinstance(item.get("url"), str):
                        url = safe_result_download_url(item["url"])
                        # No API credentials are forwarded to image storage.
                        with client.stream("GET", url) as download:
                            download.raise_for_status()
                            buffer = bytearray()
                            for chunk in download.iter_bytes():
                                buffer.extend(chunk)
                                if len(buffer) > _MAX_IMAGE_BYTES:
                                    raise ValueError("图片过大")
                            content = bytes(buffer)
                    else:
                        raise ValueError("缺少图片内容")
                    media_type, extension, (width, height) = self._image_info(content)
                    assets.append(
                        GeneratedAsset(
                            content=content,
                            media_type=media_type,
                            extension=extension,
                            provider_id=self.status.provider_id,
                            model=self.model,
                            metadata={
                                "candidate_index": index,
                                "size": f"{width}x{height}",
                                "requested_size": size,
                                "usage": {
                                    key: value
                                    for key, value in result.get("usage", {}).items()
                                    if isinstance(value, int)
                                }
                                if isinstance(result.get("usage"), dict)
                                else {},
                            },
                        )
                    )
        except httpx.HTTPError as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except (
            ValueError,
            TypeError,
            KeyError,
            binascii.Error,
            OSError,
            Image.DecompressionBombError,
        ) as exc:
            raise ProviderCallError(
                f"{self.status.provider_id} 返回的图片结果无效",
                code="provider_invalid_response",
            ) from exc
        return assets
