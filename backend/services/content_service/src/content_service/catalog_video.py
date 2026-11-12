"""Vertical product promo video: moving photo clips, TTS narration, timed subtitles, optional music.

Each photo becomes a short clip with a slow zoom over a blurred fill of itself
(no black bars on square product shots). The shared FFmpeg composer then adds
crossfades, the narration, burned-in subtitles and background music.
"""

import logging
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .media_adapters import render_srt
from .providers import (
    MusicGenerationRequest,
    SpeechSynthesisRequest,
    SubtitleAlignmentRequest,
    VideoClip,
    VideoCompositionRequest,
)

logger = logging.getLogger(__name__)
WIDTH, HEIGHT, FPS = 720, 1280, 25
# The shared composer only dips to black between shots; hard cuts read better on motion footage.
TRANSITION = 0.0
MAX_CLIPS = 6
MIN_CLIP_SECONDS = 2.5
FONT = Path(__file__).parent / "chat_fonts" / "NotoSansSC-Regular.ttf"
SUBTITLE_STYLE = {"font_size": 12, "outline_width": 1.6, "shadow": 0.6, "alignment": 2, "margin_v": 48,
                  "bold": True, "primary_color": "#FFFFFF", "outline_color": "#111111"}


@dataclass
class VideoResult:
    path: Path
    duration: float
    narrated: bool
    music: bool
    notes: list[str]


def narration_text(title: str, body: str) -> str:
    text = f"{title.strip()}。{body.strip()}" if title.strip() else body.strip()
    text = re.sub(r"#\S+", "", text)  # hashtags are for the post, not the voice
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:600]


def _title_lines(draw, title: str, font, width: int) -> list[str]:
    """At most two lines: split at a separator if there is one, otherwise into balanced halves."""
    if draw.textlength(title, font=font) <= width:
        return [title]
    for separator in ("｜", "|", "：", ":", "——", "，"):
        head, found, tail = title.partition(separator)
        if found and head and tail and max(draw.textlength(head, font=font), draw.textlength(tail, font=font)) <= width:
            return [head.strip(), tail.strip()]
    half = draw.textlength(title, font=font) / 2
    lines, current = [], ""
    for char in title:
        if current and draw.textlength(current + char, font=font) > min(width, half + font.size):
            lines.append(current)
            current = char
            if len(lines) == 2:
                return lines
        else:
            current += char
    return [*lines, current][:2]


def _frame(source: Path, target: Path, title: str) -> Path:
    """2x-size still: darkened blurred fill, the whole photo, and the title in the top band."""
    size = (WIDTH * 2, HEIGHT * 2)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        background = ImageOps.fit(image, size).filter(ImageFilter.GaussianBlur(48))
        background = ImageEnhance.Brightness(background).enhance(0.55)
        photo = ImageOps.contain(image, (int(size[0] * 0.9), int(size[1] * 0.6)))
        left = (size[0] - photo.width) // 2
        top = int(size[1] * 0.45) - photo.height // 2
        background.paste(photo, (left, max(top, 40)))
    if title:
        draw = ImageDraw.Draw(background)
        font = ImageFont.truetype(str(FONT), 84)
        y = int(size[1] * 0.06)
        for line in _title_lines(draw, title, font, int(size[0] * 0.86)):
            x = (size[0] - draw.textlength(line, font=font)) / 2
            draw.text((x, y), line, font=font, fill="#ffffff", stroke_width=3, stroke_fill="#1d1d1d")
            y += 112
    background.save(target, format="JPEG", quality=92)
    return target


def _title_overlay(title: str, target: Path) -> Path:
    """Transparent full-frame PNG with the same title band the photo shots get."""
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.truetype(str(FONT), 42)
    lines = _title_lines(draw, title, font, int(WIDTH * 0.86))
    if lines:
        band = Image.new("RGBA", (WIDTH, 60 + 56 * len(lines)), (0, 0, 0, 0))
        band_draw = ImageDraw.Draw(band)
        for row in range(band.height):  # soft dark fade so the title reads on bright footage
            band_draw.line([(0, row), (WIDTH, row)], fill=(0, 0, 0, int(110 * (1 - row / band.height))))
        overlay.alpha_composite(band)
        y = int(HEIGHT * 0.06)
        for line in lines:
            x = (WIDTH - draw.textlength(line, font=font)) / 2
            draw.text((x, y), line, font=font, fill="#ffffff", stroke_width=2, stroke_fill="#1d1d1d")
            y += 56
    overlay.save(target)
    return target


def _prepare_ai_clip(source: Path, overlay: Path, target: Path, ffmpeg: str, timeout: float) -> Path:
    graph = (f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},"
             f"fps={FPS},setsar=1[v];[v][1:v]overlay=0:0,format=yuv420p")
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-i", str(overlay), "-filter_complex", graph, "-an", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "20", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(503, "AI 镜头处理失败或超时，请检查 FFmpeg 后重试") from None
    if result.returncode or not target.is_file():
        raise HTTPException(503, "AI 镜头处理失败，请检查 FFmpeg 后重试")
    return target


def _motion_clip(frame: Path, target: Path, seconds: float, index: int, ffmpeg: str, timeout: float) -> Path:
    frames = max(1, round(seconds * FPS))
    # Alternate slow push-in and pull-out so consecutive shots don't feel identical.
    zoom = f"1+0.07*on/{frames}" if index % 2 == 0 else f"1.07-0.07*on/{frames}"
    filters = (f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}"
               f":s={WIDTH}x{HEIGHT}:fps={FPS},format=yuv420p")
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(frame),
               "-vf", filters, "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "20", "-an", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(503, "视频镜头生成失败或超时，请检查 FFmpeg 后重试") from None
    if result.returncode or not target.is_file():
        raise HTTPException(503, "视频镜头生成失败，请检查 FFmpeg 后重试")
    return target


def _duration(path: Path, ffmpeg: str) -> float | None:
    """Read the length from ffmpeg's banner; ffprobe is not always shipped."""
    try:
        output = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, timeout=30,
                                check=False).stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _narration(text: str, directory: Path, ffmpeg: str):
    from .provider_dependencies import get_speech_synthesis_port

    port = get_speech_synthesis_port()
    if not port.status.enabled or port.status.mode == "mock":
        return None, None, "未配置真实配音服务，视频没有旁白"
    try:
        result = port.synthesize(SpeechSynthesisRequest(text=text, audio_format="mp3"))
    except Exception as exc:  # any provider failure falls back to a silent cut
        logger.warning("catalog narration failed: %s", exc)
        return None, None, "配音服务这次没有成功，视频暂无旁白，可重新生成视频"
    path = directory / f"narration.{result.extension or 'mp3'}"
    path.write_bytes(result.audio)
    duration = result.duration_seconds or _duration(path, ffmpeg)
    if not duration:
        return None, None, "配音时长无法识别，视频暂无旁白"
    return (path, duration, result.word_timings), None, None


def _music(seconds: float, directory: Path):
    from .provider_dependencies import get_music_generation_port, get_runtime_settings

    port = get_music_generation_port()
    if not port.status.enabled or port.status.mode == "mock":
        return None, "未配置背景音乐服务"
    settings = get_runtime_settings()
    target = min(max(seconds, settings.music_min_duration_seconds), settings.music_max_duration_seconds)
    try:
        result = port.generate(MusicGenerationRequest(
            prompt="沉稳大气、有质感的器乐，适合商品展示短视频，节奏舒缓，无人声", duration_seconds=target))
    except Exception as exc:  # music is optional
        logger.warning("catalog music failed: %s", exc)
        return None, "背景音乐服务这次没有成功，视频只用了配音"
    path = directory / f"music.{result.extension}"
    path.write_bytes(result.audio)
    return path, None


def render_product_video(images: list[Path], title: str, body: str, directory: Path, settings, *,
                         voice: bool = True, music: bool = False, ai_clips: list[Path] | None = None,
                         narration_override: str | None = None) -> VideoResult:
    import imageio_ffmpeg

    from .provider_dependencies import get_subtitle_alignment_port, get_video_composer_port

    if not images and not ai_clips:
        raise HTTPException(422, "没有可用于视频的图片")
    ffmpeg = settings.ffmpeg_path or imageio_ffmpeg.get_ffmpeg_exe()
    timeout = min(settings.ffmpeg_timeout_seconds, 240)
    notes: list[str] = []
    narration = None
    text = narration_override or narration_text(title, body)
    if voice and text:
        narration, _, note = _narration(text, directory, ffmpeg)
        if note:
            notes.append(note)
    clips: list[VideoClip] = []

    def add(path: Path, seconds: float) -> None:
        clips.append(VideoClip(path, seconds, "none", TRANSITION, is_image=False, fit_mode="cover"))

    def add_photos(sources: list[Path], seconds: float) -> None:
        for source in sources:
            index = len(clips)
            frame = _frame(Path(source), directory / f"frame-{index + 1:02d}.jpg", title.strip())
            add(_motion_clip(frame, directory / f"shot-{index + 1:02d}.mp4", seconds, index, ffmpeg, timeout),
                seconds)
            frame.unlink(missing_ok=True)

    target_seconds = narration[1] + 0.8 if narration else 0.0
    if ai_clips:
        overlay = _title_overlay(title.strip(), directory / "title-overlay.png")
        for index, source in enumerate(ai_clips):
            prepared = _prepare_ai_clip(source, overlay, directory / f"ai-shot-{index + 1:02d}.mp4", ffmpeg, timeout)
            add(prepared, _duration(prepared, ffmpeg) or 5.0)
        overlay.unlink(missing_ok=True)
        covered = sum(clip.duration_seconds for clip in clips) - TRANSITION * (len(clips) - 1)
        deficit = target_seconds - covered
        if deficit > 0.5 and images:
            # Narration runs longer than the clips: continue with the product photos.
            extra = images[:max(1, min(len(images), math.ceil(deficit / 4)))]
            add_photos(extra, max(MIN_CLIP_SECONDS, (deficit + len(extra) * TRANSITION) / len(extra)))
        total = max(target_seconds, sum(clip.duration_seconds for clip in clips) - TRANSITION * (len(clips) - 1))
    else:
        images = images[:MAX_CLIPS]
        total = target_seconds or max(9.0, 3.2 * len(images))
        add_photos(images, max(MIN_CLIP_SECONDS, (total + (len(images) - 1) * TRANSITION) / len(images)))

    subtitle_path = None
    if narration and text:
        path, duration, timings = narration
        segments = get_subtitle_alignment_port().align(SubtitleAlignmentRequest(
            text=text, duration_seconds=duration, word_timings=tuple(timings or ()),
            max_chars_per_line=14, max_lines=2))
        subtitle_path = directory / "subtitles.srt"
        subtitle_path.write_text(render_srt(segments), encoding="utf-8")

    music_path = None
    if music:
        music_path, note = _music(total, directory)
        if note:
            notes.append(note)

    target = directory / "video.mp4"
    result = get_video_composer_port().compose(VideoCompositionRequest(
        clips=tuple(clips), output_path=target, width=WIDTH, height=HEIGHT, fps=FPS, fit_mode="cover",
        source_audio_policy="replace_by_voice" if narration else "mute",
        voice_path=narration[0] if narration else None, subtitle_path=subtitle_path,
        subtitle_style=SUBTITLE_STYLE if subtitle_path else {},
        background_music_path=music_path, background_music_volume=0.16 if narration else 0.6,
    ))
    for clip in clips:
        clip.path.unlink(missing_ok=True)
    if not target.is_file() or target.stat().st_size < 1000:
        raise HTTPException(503, "视频合成失败，请检查 FFmpeg 后重试")
    return VideoResult(target, result.duration_seconds, bool(narration), bool(music_path), notes)
