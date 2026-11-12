"""GPT Image through the Responses API ``image_generation`` tool.

Some OpenAI-compatible relays (for example ones backed by ChatGPT/Codex
accounts) do not serve /images/generations but do run the hosted image tool
inside /responses. A chat model drives the call; ``model`` is the image model
the tool uses, so the usual fallback across image models still applies.
"""

from __future__ import annotations

import base64
import binascii
import io
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import httpx
from PIL import Image, ImageOps

from .llm_adapters import ProviderCallError
from .media_adapters import _provider_endpoint, _safe_http_error
from .openai_image_adapter import (
    _MAX_IMAGE_BYTES,
    _MAX_REFERENCES,
    OpenAICompatibleImageGenerationPort,
)
from .providers import GeneratedAsset, ImageGenerationRequest

_REFERENCE_EDGE = 1024


def _reference_data_url(content: bytes) -> str:
    """Downscale references so a request with several product photos stays small."""
    with Image.open(io.BytesIO(content)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((_REFERENCE_EDGE, _REFERENCE_EDGE))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class OpenAIResponsesImageGenerationPort(OpenAICompatibleImageGenerationPort):
    def __init__(self, *, driver_model: str, quality: str = "high", **kwargs: Any):
        super().__init__(**kwargs)
        if not driver_model:
            raise ValueError("请填写调用生图工具的对话模型")
        self.endpoint = _provider_endpoint(kwargs["base_url"], "/responses")
        self.driver_model = driver_model
        self.quality = quality
        self.status = replace(self.status, name="openai-responses-image")

    def _validate(self, request: ImageGenerationRequest) -> None:
        if not request.prompt.strip():
            raise ValueError("图片提示词不能为空")
        if not 1 <= request.count <= self.max_candidates:
            raise ValueError(f"单次图片候选数量必须在 1 到 {self.max_candidates} 之间")
        if request.watermark or request.seed is not None or request.extra:
            raise ValueError("当前图片模型不支持水印、随机种子或额外参数")
        if len(request.reference_images) > _MAX_REFERENCES:
            raise ValueError(f"单次图片生成最多允许 {_MAX_REFERENCES} 张参考图片")
        for content in request.reference_images:
            try:
                self._image_info(content)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise ValueError("参考图片必须是有效的 JPEG、PNG 或 WebP，且不超过 30MB") from exc

    def _payload(self, request: ImageGenerationRequest, size: str) -> dict[str, Any]:
        tool: dict[str, Any] = {"type": "image_generation", "model": self.model, "quality": self.quality}
        if size != "auto":
            tool["size"] = size
        content: list[dict[str, Any]] = [{"type": "input_text", "text": request.prompt}]
        content += [{"type": "input_image", "image_url": _reference_data_url(item)}
                    for item in request.reference_images]
        return {
            "model": self.driver_model,
            "input": [{"role": "user", "content": content}],
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
        }

    def generate(self, request: ImageGenerationRequest) -> Sequence[GeneratedAsset]:
        self._validate(request)
        size = self._size(request)
        payload = self._payload(request, size)
        assets = []
        try:
            with httpx.Client(timeout=self.timeout_seconds, trust_env=False, follow_redirects=False) as client:
                for index in range(request.count):
                    response = client.post(self.endpoint, headers={"Authorization": f"Bearer {self.api_key}"},
                                           json=payload)
                    response.raise_for_status()
                    result = response.json()
                    calls = [item for item in result.get("output", []) if isinstance(item, dict)
                             and item.get("type") == "image_generation_call"] if isinstance(result, dict) else []
                    encoded = calls[0].get("result") if calls else None
                    if not isinstance(encoded, str) or not encoded:
                        raise ValueError("缺少图片结果")
                    if len(encoded) > 4 * ((_MAX_IMAGE_BYTES + 2) // 3):
                        raise ValueError("图片过大")
                    content = base64.b64decode(encoded, validate=True)
                    media_type, extension, (width, height) = self._image_info(content)
                    assets.append(GeneratedAsset(
                        content=content, media_type=media_type, extension=extension,
                        provider_id=self.status.provider_id, model=self.model,
                        metadata={"candidate_index": index, "size": f"{width}x{height}",
                                  "requested_size": size, "quality": calls[0].get("quality") or self.quality},
                    ))
        except httpx.HTTPError as exc:
            raise _safe_http_error(self.status.provider_id, exc) from exc
        except (ValueError, TypeError, KeyError, binascii.Error, OSError, Image.DecompressionBombError) as exc:
            raise ProviderCallError(f"{self.status.provider_id} 返回的图片结果无效") from exc
        return assets
