from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal

MediaKind = Literal["image", "video", "audio", "subtitle"]


class MediaValidationError(ValueError):
    """Raised when an untrusted upload does not match the supported media policy."""


def run_captured_process(
    command: list[str],
    *,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg/ffprobe without using the Windows default GBK text decoder."""
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


@dataclass(frozen=True, slots=True)
class MediaIngestPolicy:
    max_bytes: dict[MediaKind, int] = field(
        default_factory=lambda: {
            "image": 100 * 1024 * 1024,
            "video": 4 * 1024 * 1024 * 1024,
            "audio": 1024 * 1024 * 1024,
            "subtitle": 10 * 1024 * 1024,
        }
    )
    max_probe_seconds: float = 30.0
    max_derivative_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class MediaProbe:
    kind: MediaKind
    media_type: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    channels: int | None = None
    sample_rate: int | None = None
    format_name: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class GeneratedDerivative:
    kind: Literal["proxy", "thumbnail", "waveform"]
    path: Path
    media_type: str
    metadata: dict[str, object] = field(default_factory=dict)


_EXTENSION_KIND: dict[str, MediaKind] = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".m4v": "video",
    ".webm": "video",
    ".mkv": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".flac": "audio",
    ".srt": "subtitle",
    ".vtt": "subtitle",
    ".txt": "subtitle",
}

_KIND_MEDIA_TYPES: dict[MediaKind, frozenset[str]] = {
    "image": frozenset({"image/jpeg", "image/png", "image/webp"}),
    "video": frozenset(
        {
            "video/mp4",
            "video/quicktime",
            "video/webm",
            "video/x-matroska",
        }
    ),
    "audio": frozenset(
        {
            "audio/mpeg",
            "audio/wav",
            "audio/x-wav",
            "audio/mp4",
            "audio/aac",
            "audio/ogg",
            "audio/flac",
        }
    ),
    "subtitle": frozenset(
        {
            "application/x-subrip",
            "text/srt",
            "text/vtt",
            "text/plain",
        }
    ),
}


def safe_filename(value: str | None) -> str:
    """Return a basename safe for object-storage keys and response headers."""

    name = Path(value or "material.bin").name
    name = re.sub(r"[^\w.\-()\u4e00-\u9fff]+", "_", name, flags=re.UNICODE)
    name = name.strip("._")
    return name[:180] or "material.bin"


def _signature_kind(head: bytes) -> MediaKind | None:
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return "image"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        major = head[8:12]
        return "audio" if major in {b"M4A ", b"M4B ", b"M4P "} else "video"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio"
    if head.startswith((b"ID3", b"fLaC", b"OggS")):
        return "audio"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "audio"
    return None


def _looks_like_subtitle(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    normalized = text.lstrip()
    return bool(
        normalized
        and (
            normalized.startswith("WEBVTT")
            or "-->" in normalized
            or all(character.isprintable() or character in "\r\n\t" for character in normalized)
        )
    )


def validate_upload_header(
    *,
    filename: str | None,
    declared_media_type: str | None,
    head: bytes,
    size_bytes: int | None = None,
    policy: MediaIngestPolicy | None = None,
) -> tuple[MediaKind, str]:
    """Validate extension, declared MIME, signature and configured size limit.

    Text subtitle formats intentionally use a strict UTF-8/no-NUL check because they
    do not have a binary magic signature.  The return value is the canonical kind and
    MIME type; callers must store that MIME instead of trusting the request header.
    """

    current_policy = policy or MediaIngestPolicy()
    extension = Path(filename or "").suffix.lower()
    extension_kind = _EXTENSION_KIND.get(extension)
    if extension_kind is None:
        raise MediaValidationError("不支持的素材扩展名")
    signature_kind = _signature_kind(head)
    if extension_kind == "subtitle":
        if not _looks_like_subtitle(head):
            raise MediaValidationError("字幕文件必须是 UTF-8 文本，且不能包含二进制内容")
        signature_kind = "subtitle"
    if signature_kind != extension_kind:
        raise MediaValidationError("文件内容与扩展名不一致")

    declared = (declared_media_type or "").split(";", 1)[0].strip().lower()
    allowed_types = _KIND_MEDIA_TYPES[extension_kind]
    if declared and declared != "application/octet-stream" and declared not in allowed_types:
        raise MediaValidationError("文件内容与声明的媒体类型不一致")
    if size_bytes is not None:
        if size_bytes < 1:
            raise MediaValidationError("素材文件不能为空")
        if size_bytes > current_policy.max_bytes[extension_kind]:
            raise MediaValidationError("素材文件超过当前类型的大小限制")

    canonical_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".srt": "application/x-subrip",
        ".vtt": "text/vtt",
        ".txt": "text/plain",
    }[extension]
    return extension_kind, canonical_type


def read_upload_head(stream: BinaryIO, limit: int = 8192) -> bytes:
    """Read a signature prefix and restore the original stream position."""

    position = stream.tell()
    head = stream.read(limit)
    stream.seek(position)
    return head


def _parse_rate(value: object) -> float | None:
    raw = str(value or "")
    if not raw or raw in {"0/0", "N/A"}:
        return None
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        try:
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else None
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        return None


def _ffmpeg_executable(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None
    return bundled if Path(bundled).is_file() else None


def _probe_media_with_ffmpeg(
    path: Path,
    *,
    kind: MediaKind,
    media_type: str,
    timeout_seconds: float,
) -> MediaProbe:
    executable = _ffmpeg_executable()
    if not executable:
        raise RuntimeError("ffprobe 与 FFmpeg 均不可用，无法安全探测媒体")
    completed = run_captured_process(
        [executable, "-hide_banner", "-i", str(path)],
        timeout=timeout_seconds,
    )
    detail = completed.stderr or ""
    stream_lines = [line for line in detail.splitlines() if "Stream #" in line]
    video_line = next((line for line in stream_lines if "Video:" in line), "")
    audio_line = next((line for line in stream_lines if "Audio:" in line), "")
    if kind == "video" and not video_line:
        raise MediaValidationError("视频文件不包含可解码的视频流")
    if kind == "audio" and not audio_line:
        raise MediaValidationError("音频文件不包含可解码的音频流")

    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):([0-9]+(?:\.[0-9]+)?)",
        detail,
    )
    duration = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    dimensions = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", video_line)
    fps_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s+fps", video_line)
    video_codec = re.search(r"Video:\s*([^,\s]+)", video_line)
    audio_codec = re.search(r"Audio:\s*([^,\s]+)", audio_line)
    sample_rate = re.search(r"(\d+)\s+Hz", audio_line)
    channels = 1 if " mono" in audio_line else 2 if " stereo" in audio_line else None
    return MediaProbe(
        kind=kind,
        media_type=media_type,
        duration_seconds=duration,
        width=int(dimensions.group(1)) if dimensions else None,
        height=int(dimensions.group(2)) if dimensions else None,
        fps=float(fps_match.group(1)) if fps_match else None,
        video_codec=video_codec.group(1) if video_codec else None,
        audio_codec=audio_codec.group(1) if audio_codec else None,
        channels=channels,
        sample_rate=int(sample_rate.group(1)) if sample_rate else None,
        size_bytes=path.stat().st_size,
    )


def probe_media(
    path: Path,
    *,
    kind: MediaKind,
    media_type: str,
    ffprobe_path: str | None = None,
    timeout_seconds: float = 30.0,
) -> MediaProbe:
    if kind == "subtitle":
        return MediaProbe(kind=kind, media_type=media_type, size_bytes=path.stat().st_size)
    executable = ffprobe_path or shutil.which("ffprobe")
    if not executable:
        return _probe_media_with_ffmpeg(
            path,
            kind=kind,
            media_type=media_type,
            timeout_seconds=timeout_seconds,
        )
    completed = run_captured_process(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,size:stream=codec_type,codec_name,width,height,avg_frame_rate,channels,sample_rate",
            "-of",
            "json",
            str(path),
        ],
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "媒体探测失败").strip()[:500]
        raise MediaValidationError(detail)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaValidationError("ffprobe 返回了无效结果") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else []
    streams = streams if isinstance(streams, list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    if kind == "video" and not video:
        raise MediaValidationError("视频文件不包含可解码的视频流")
    if kind == "audio" and not audio:
        raise MediaValidationError("音频文件不包含可解码的音频流")
    format_info = payload.get("format") if isinstance(payload, dict) else {}
    format_info = format_info if isinstance(format_info, dict) else {}
    try:
        duration = float(format_info.get("duration"))
    except (TypeError, ValueError):
        duration = None
    return MediaProbe(
        kind=kind,
        media_type=media_type,
        duration_seconds=duration,
        width=_optional_int(video.get("width")),
        height=_optional_int(video.get("height")),
        fps=_parse_rate(video.get("avg_frame_rate")),
        video_codec=str(video.get("codec_name") or "") or None,
        audio_codec=str(audio.get("codec_name") or "") or None,
        channels=_optional_int(audio.get("channels")),
        sample_rate=_optional_int(audio.get("sample_rate")),
        format_name=str(format_info.get("format_name") or "") or None,
        size_bytes=_optional_int(format_info.get("size")) or path.stat().st_size,
    )


def generate_derivatives(
    source_path: Path,
    output_directory: Path,
    *,
    probe: MediaProbe,
    ffmpeg_path: str | None = None,
    timeout_seconds: float = 600.0,
) -> tuple[GeneratedDerivative, ...]:
    """Create deterministic browser derivatives without shell interpolation."""

    executable = _ffmpeg_executable(ffmpeg_path)
    if not executable:
        raise RuntimeError("FFmpeg 不可用，无法生成代理素材")
    output_directory.mkdir(parents=True, exist_ok=True)
    results: list[GeneratedDerivative] = []

    def run(arguments: list[str]) -> None:
        completed = run_captured_process(
            [executable, "-hide_banner", "-loglevel", "error", "-y", *arguments],
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            raise MediaValidationError((completed.stderr or "代理素材生成失败").strip()[:1000])

    if probe.kind == "video":
        proxy_path = output_directory / "proxy-480p.mp4"
        run(
            [
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                "scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "26",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(proxy_path),
            ]
        )
        results.append(
            GeneratedDerivative(
                kind="proxy",
                path=proxy_path,
                media_type="video/mp4",
                metadata={"width": 854, "height": 480},
            )
        )
        thumbnail_path = output_directory / "thumbnail.jpg"
        run(
            [
                "-ss",
                "0.1",
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-2:force_original_aspect_ratio=decrease",
                "-q:v",
                "3",
                str(thumbnail_path),
            ]
        )
        results.append(GeneratedDerivative("thumbnail", thumbnail_path, "image/jpeg"))
    elif probe.kind == "image":
        thumbnail_path = output_directory / "thumbnail.jpg"
        run(
            [
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=640:640:force_original_aspect_ratio=decrease",
                "-q:v",
                "3",
                str(thumbnail_path),
            ]
        )
        results.append(GeneratedDerivative("thumbnail", thumbnail_path, "image/jpeg"))

    if probe.kind in {"video", "audio"} and probe.audio_codec:
        waveform_path = output_directory / "waveform.png"
        run(
            [
                "-i",
                str(source_path),
                "-filter_complex",
                "aformat=channel_layouts=mono,showwavespic=s=1600x180:colors=0x17806f",
                "-frames:v",
                "1",
                str(waveform_path),
            ]
        )
        results.append(
            GeneratedDerivative(
                "waveform",
                waveform_path,
                "image/png",
                {"width": 1600, "height": 180},
            )
        )
    return tuple(results)
