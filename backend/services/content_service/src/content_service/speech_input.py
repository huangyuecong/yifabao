"""Speech-to-text for the AI chat input (OpenAI-compatible /audio/transcriptions).

The recording is forwarded once and never stored. Configure CONTENT_ASR_MODEL; the
endpoint and key default to the text model gateway and can be overridden with
CONTENT_ASR_BASE_URL / CONTENT_ASR_API_KEY.
"""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException

from .config import Settings

MAX_AUDIO_BYTES = 10 * 1024 * 1024
TIMEOUT_SECONDS = 60
AUDIO_TYPES = ("audio/", "video/webm")  # Chromium may label an audio-only recording video/webm


def configuration(settings: Settings) -> dict | None:
    model = os.getenv("CONTENT_ASR_MODEL", "").strip()
    base = (os.getenv("CONTENT_ASR_BASE_URL", "").strip() or settings.llm_base_url or "").rstrip("/")
    key = os.getenv("CONTENT_ASR_API_KEY", "").strip() or settings.llm_api_key or ""
    if not (model and base and key):
        return None
    base = base.removesuffix("/chat/completions")
    if not base.endswith("/v1"):
        base += "/v1"
    return {"endpoint": f"{base}/audio/transcriptions", "key": key, "model": model}


def available(settings: Settings) -> bool:
    return configuration(settings) is not None


def transcribe(settings: Settings, content: bytes, filename: str, content_type: str) -> str:
    """Return the recognized text; raise HTTPException with a user-facing message otherwise."""
    config = configuration(settings)
    if config is None:
        raise HTTPException(503, "语音输入尚未配置，请管理员设置语音识别模型（CONTENT_ASR_MODEL）")
    if not content:
        raise HTTPException(422, "录音为空，请重试")
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "录音过长，请分段录制，每段 1 分钟以内")
    if not content_type.startswith(AUDIO_TYPES):
        raise HTTPException(422, "请上传录音文件")
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            response = client.post(
                config["endpoint"],
                headers={"Authorization": f"Bearer {config['key']}"},
                files={"file": (filename, content, content_type)},
                data={"model": config["model"], "language": "zh"},
            )
    except httpx.HTTPError:
        raise HTTPException(502, "语音识别服务暂时连不上，请稍后重试或直接输入文字") from None
    if response.status_code != 200:
        raise HTTPException(502, f"语音识别失败（HTTP {response.status_code}），请重试或直接输入文字")
    try:
        text = response.json().get("text")
    except (ValueError, AttributeError):
        raise HTTPException(502, "语音识别服务返回的内容无法识别，请重试") from None
    return str(text or "").strip()
