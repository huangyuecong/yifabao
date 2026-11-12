"""Seedance image-to-video clips and a short voiceover script for catalog products.

All clips of one product are submitted at once and polled together. Any clip
that fails or times out is skipped; the caller falls back to photo animation
when none succeed.
"""

import base64
import io
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from .providers import AiVideoGenerationRequest

logger = logging.getLogger(__name__)
CLIP_SECONDS = 5
POLL_SECONDS = 10
DEADLINE_SECONDS = 15 * 60
SHOTS = (
    "镜头缓慢推近商品主体，柔和暖光掠过瓶身，突出整体造型与质感",
    "镜头沿水平方向缓慢平移，展示瓶身纹饰与包装细节，光影轻微流动",
    "镜头缓慢拉远，呈现商品与包装的完整陈列，氛围沉稳高级",
)
GUARD = ("以画面中的商品为绝对主体，严格保持瓶型、颜色、包装结构、品牌标识和标签文字与原图一致，"
         "不替换、不变形，不新增任何文字、字幕或水印；不出现人物；画面稳定不晃动。")


@dataclass
class ClipResult:
    paths: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    model: str = ""


def _data_url(path: Path) -> str:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1024, 1024))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _reason(exc: Exception) -> str:
    message = str(getattr(exc, "message", "") or exc)
    return message[:80] or type(exc).__name__


def generate_ai_clips(images: list[Path], product_name: str, instructions: str, count: int, directory: Path, *,
                      port=None, sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic) -> ClipResult:
    from .media_adapters import download_generated_video
    from .provider_dependencies import get_ai_video_generation_port, get_asset_store_port

    result = ClipResult()
    try:
        port = port or get_ai_video_generation_port()
    except Exception as exc:  # misconfigured provider: fall back to photo animation
        result.notes.append(f"AI 视频服务不可用（{_reason(exc)}）")
        return result
    if not port.status.enabled or port.status.mode == "mock" or not images:
        result.notes.append("未配置真实的 AI 视频服务")
        return result
    submitted = []
    for index in range(count):
        prompt = f"{product_name}。{SHOTS[index % len(SHOTS)]}。{GUARD}"
        if instructions:
            prompt += f"补充要求：{instructions[:300]}"
        try:
            task = port.submit(AiVideoGenerationRequest(
                prompt=prompt, image_url=_data_url(images[index % len(images)]), duration_seconds=CLIP_SECONDS,
                ratio="9:16", resolution="720p", first_frame=True))
            submitted.append((index, task))
            result.model = result.model or task.model
        except Exception as exc:
            logger.warning("catalog AI clip submit failed: %s", exc)
            result.notes.append(f"第 {index + 1} 段镜头提交失败：{_reason(exc)}")
    pending = {task.provider_task_id: (index, task) for index, task in submitted}
    finished = {}
    deadline = clock() + DEADLINE_SECONDS
    while pending and clock() < deadline:
        sleep(POLL_SECONDS)
        for task_id in list(pending):
            try:
                current = port.get(task_id)
            except Exception as exc:  # transient polling error: keep waiting
                logger.info("catalog AI clip poll failed: %s", exc)
                continue
            if current.status not in ("queued", "running"):
                finished[task_id] = current
                pending.pop(task_id)
    for index, _ in pending.values():
        result.notes.append(f"第 {index + 1} 段镜头超时未完成")
    store = get_asset_store_port()
    for index, task in submitted:
        current = finished.get(task.provider_task_id)
        if current is None:
            continue
        if current.status != "succeeded":
            result.notes.append(f"第 {index + 1} 段镜头生成失败：{(current.error_message or current.status)[:80]}")
            continue
        key = f"catalog/ai-clips/{uuid4().hex}.mp4"
        try:
            download_generated_video(current, asset_store=store, storage_key=key)
            target = directory / f"ai-clip-{index + 1:02d}.mp4"
            shutil.move(str(store.path_for(key)), target)
            result.paths.append(target)
        except Exception as exc:
            logger.warning("catalog AI clip download failed: %s", exc)
            result.notes.append(f"第 {index + 1} 段镜头下载失败")
    return result


def _first_sentences(title: str, body: str, limit: int = 70) -> str:
    text = title.strip().rstrip("。") + "。" if title.strip() else ""
    for sentence in re.findall(r"[^。！？!?]+[。！？!?]?", body):
        if len(text) + len(sentence) > limit:
            break
        text += sentence.strip()
    return text or body[:limit]


def short_voiceover(title: str, body: str, rules: list[str]) -> str:
    """About 15 seconds of spoken copy for 2-3 clips; falls back to the opening sentences."""
    from .ai_model_config import model_settings
    from .chat_provider import call_chat
    from .provider_dependencies import get_runtime_settings

    fallback = _first_sentences(title, re.sub(r"#\S+", "", body))
    try:
        settings = model_settings(get_runtime_settings(), "text")
        if settings.llm_provider == "mock":
            return fallback
        reply = call_chat(settings, {"model": settings.llm_model}, [
            {"role": "system", "content": (
                "你是短视频口播撰稿人。把用户给的宣传文案改写成约 15 秒的口播稿，45 到 65 个汉字，口语化、有节奏。"
                "只使用文案里已有的事实，不新增价格、功效、承诺、促销或排名；不要话题标签、表情和引号。只输出口播稿正文。")},
            {"role": "user", "content": f"标题：{title}\n正文：{body}"},
        ])
    except Exception as exc:
        logger.info("catalog voiceover rewrite failed: %s", exc)
        return fallback
    script = re.sub(r"\s+", "", reply.strip().strip("“”\"'"))
    if not 20 <= len(script) <= 90 or any(term and term.casefold() in script.casefold() for term in rules):
        return fallback
    return script
