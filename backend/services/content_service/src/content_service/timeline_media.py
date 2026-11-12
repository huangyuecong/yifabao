from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Literal

from .providers import ProviderStatus

type TimelineMediaKind = Literal["video", "image"]
type TimelineAudioKind = Literal["source", "voice", "music", "sfx"]

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"}
_SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
_FIT_MODES = {"contain", "cover"}
_SOURCE_AUDIO_POLICIES = {"keep", "mute"}
_AUDIO_KINDS = {"voice", "music", "sfx"}
_MIN_AUDIO_SPEED = 0.1
_MAX_AUDIO_SPEED = 8.0
_TRANSITIONS = {
    "fade": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "dissolve": "dissolve",
    "wipeleft": "wipeleft",
    "wiperight": "wiperight",
    "wipeup": "wipeup",
    "wipedown": "wipedown",
    "slideleft": "slideleft",
    "slideright": "slideright",
    "slideup": "slideup",
    "slidedown": "slidedown",
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
    "smoothup": "smoothup",
    "smoothdown": "smoothdown",
    "circlecrop": "circlecrop",
    "circleopen": "circleopen",
    "circleclose": "circleclose",
    "pixelize": "pixelize",
}
_LOGO_POSITIONS = {
    "top_left": "24:24",
    "top_right": "W-w-24:24",
    "bottom_left": "24:H-h-24",
    "bottom_right": "W-w-24:H-h-24",
    "center": "(W-w)/2:(H-h)/2",
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
_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True, slots=True)
class TimelineClip:
    """One ordered primary-video clip.

    Repeating the same ``asset_id``/``source_path`` with different source ranges
    is the canonical representation of a split operation. ``transition`` is
    the transition from the previous clip into this clip.

    Paths are internal, server-resolved fields. Public request schemas should
    accept asset IDs only and resolve them before constructing this object.
    """

    asset_id: str
    source_path: Path
    kind: TimelineMediaKind = "video"
    source_in_seconds: float = 0
    source_out_seconds: float | None = None
    duration_seconds: float | None = None
    transition: str = "none"
    transition_duration_seconds: float = 0.4
    fit_mode: Literal["contain", "cover"] = "contain"
    source_audio: Literal["keep", "mute"] = "keep"
    source_audio_volume: float = 1
    has_source_audio: bool | None = None


@dataclass(frozen=True, slots=True)
class TimelineAudioTrack:
    """A voice, music, or sound-effect item placed on the global timeline."""

    asset_id: str
    source_path: Path
    kind: Literal["voice", "music", "sfx"]
    offset_seconds: float = 0
    source_in_seconds: float = 0
    source_out_seconds: float | None = None
    duration_seconds: float | None = None
    volume: float = 1
    fade_in_seconds: float = 0
    fade_out_seconds: float = 0
    loop: bool = False
    speed: float = 1
    # Source range used for looping after a project trims the source tail.
    loop_source_duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class TimelineLogo:
    asset_id: str
    source_path: Path
    start_seconds: float = 0
    end_seconds: float | None = None
    width: int = 160
    position: Literal[
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "center",
    ] = "top_right"
    opacity: float = 1
    # Browser editing stores the overlay centre as canvas percentages.  Keep the
    # legacy named position for older revisions, but prefer these coordinates
    # whenever both are present.
    x_percent: float | None = None
    y_percent: float | None = None


@dataclass(frozen=True, slots=True)
class TimelineSubtitle:
    asset_id: str
    source_path: Path
    style: dict[str, Any] = field(default_factory=dict)
    canvas_width: int | None = None
    canvas_height: int | None = None


@dataclass(frozen=True, slots=True)
class TimelineDucking:
    enabled: bool = False
    target_kinds: tuple[TimelineAudioKind, ...] = ("music",)
    threshold: float = 0.03
    ratio: float = 8
    attack_seconds: float = 0.02
    release_seconds: float = 0.5


@dataclass(frozen=True, slots=True)
class TimelineRenderRequest:
    clips: tuple[TimelineClip, ...]
    output_path: Path
    width: int
    height: int
    fps: int = 25
    audio_tracks: tuple[TimelineAudioTrack, ...] = ()
    logos: tuple[TimelineLogo, ...] = ()
    subtitles: tuple[TimelineSubtitle, ...] = ()
    ducking: TimelineDucking = field(default_factory=TimelineDucking)
    background_color: str = "#000000"
    video_bitrate: str = "4M"
    audio_bitrate: str = "192k"
    overwrite: bool = False
    # Project-only extent for non-primary tracks. Legacy composition requests
    # retain their existing main-track duration when these controls are absent.
    duration_seconds: float | None = None
    extend_to_audio: bool = False


@dataclass(frozen=True, slots=True)
class TimelineRenderResult:
    output_path: Path
    duration_seconds: float
    width: int
    height: int
    fps: int
    clip_count: int
    audio_track_count: int
    transitions_applied: tuple[str, ...]
    source_audio_clip_count: int
    ducking_applied: bool
    logos_applied: int
    subtitles_burned: int


@dataclass(frozen=True, slots=True)
class _ResolvedClip:
    value: TimelineClip
    path: Path
    duration: float
    has_audio: bool


@dataclass(frozen=True, slots=True)
class _ResolvedAudio:
    value: TimelineAudioTrack
    path: Path
    duration: float


@dataclass(frozen=True, slots=True)
class _RenderPlan:
    command: tuple[str, ...]
    output_path: Path
    duration: float
    transitions: tuple[str, ...]
    source_audio_count: int
    ducking_applied: bool


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
    ffmpeg_path = Path(ffmpeg)
    candidate = ffmpeg_path.with_name(
        "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
    )
    if candidate.is_file():
        return str(candidate)
    return shutil.which("ffprobe")


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return not roots or any(path == root or root in path.parents for root in roots)


def _validate_asset_id(value: str) -> None:
    if not _ASSET_ID.fullmatch(value):
        raise ValueError("时间线素材 ID 格式无效")


def _format_seconds(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _atempo_filter_chain(speed: float) -> tuple[str, ...]:
    """Return an equivalent chain whose individual factors stay in FFmpeg's safe range."""

    if not math.isfinite(speed) or not _MIN_AUDIO_SPEED <= speed <= _MAX_AUDIO_SPEED:
        raise ValueError("音轨倍速必须在 0.1 到 8 之间")
    remaining = speed
    factors: list[float] = []
    while remaining < 0.5 - 0.000001:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2 + 0.000001:
        factors.append(2.0)
        remaining /= 2.0
    if abs(remaining - 1) > 0.000001:
        factors.append(remaining)
    return tuple(f"atempo={_format_seconds(factor)}" for factor in factors)


def _probe_with_ffmpeg(path: Path, ffmpeg: str | None) -> tuple[float | None, bool | None]:
    """Read lightweight stream metadata when a separate ffprobe is unavailable."""

    if not ffmpeg:
        return None, None
    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        detail = completed.stderr or ""
        duration_match = re.search(
            r"Duration:\s*(\d+):(\d+):([0-9]+(?:\.[0-9]+)?)",
            detail,
        )
        duration = None
        if duration_match:
            hours, minutes, seconds = duration_match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        stream_lines = [line for line in detail.splitlines() if "Stream #" in line]
        has_audio = any("Audio:" in line for line in stream_lines)
        has_video = any("Video:" in line for line in stream_lines)
        if not has_audio and not has_video:
            return duration, None
        return duration, has_audio
    except (subprocess.SubprocessError, OSError, ValueError):
        return None, None


def _probe(
    path: Path,
    ffprobe: str | None,
    ffmpeg: str | None = None,
) -> tuple[float | None, bool | None]:
    if not ffprobe:
        return _probe_with_ffmpeg(path, ffmpeg)
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        payload = json.loads(completed.stdout)
        raw_duration = (payload.get("format") or {}).get("duration")
        duration = float(raw_duration) if raw_duration is not None else None
        streams = payload.get("streams") or []
        has_audio = any(
            isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
        )
        return duration, has_audio
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return _probe_with_ffmpeg(path, ffmpeg)


def _ass_color(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", normalized):
        raise ValueError(f"{field_name} 必须是 #RRGGBB 颜色值")
    red, green, blue = normalized[1:3], normalized[3:5], normalized[5:7]
    return f"&H00{blue}{green}{red}".upper()


def _subtitle_force_style(
    style: dict[str, Any],
    canvas_width: int | None = None,
    canvas_height: int | None = None,
) -> str:
    if not style and canvas_width is None:
        return ""
    unknown = set(style).difference(_SUBTITLE_STYLE_KEYS)
    if unknown:
        raise ValueError(f"不支持的字幕样式参数：{', '.join(sorted(unknown))}")
    font_size = int(style.get("font_size", 28 if canvas_width else 36))
    outline_width = float(style.get("outline_width", 2))
    shadow = float(style.get("shadow", 0))
    alignment = int(style.get("alignment", 2))
    margin_v = int(style.get("margin_v", 36))
    bold = style.get("bold", False)
    if not 8 <= font_size <= 120:
        raise ValueError("字幕字号必须在 8 到 120 之间")
    if not 0 <= outline_width <= 10 or not 0 <= shadow <= 10:
        raise ValueError("字幕描边或阴影参数超出安全范围")
    if alignment not in range(1, 10):
        raise ValueError("字幕位置必须是 1 到 9")
    if not 0 <= margin_v <= 500:
        raise ValueError("字幕垂直边距必须在 0 到 500 之间")
    if not isinstance(bold, bool):
        raise TypeError("字幕粗体参数必须是布尔值")
    canvas = []
    if canvas_width is not None or canvas_height is not None:
        if (
            canvas_width is None
            or canvas_height is None
            or not 64 <= canvas_width <= 7680
            or not 64 <= canvas_height <= 4320
        ):
            raise ValueError("字幕画布尺寸无效")
        canvas = [f"PlayResX={canvas_width}", f"PlayResY={canvas_height}"]
    return ",".join(
        [
            *canvas,
            f"FontSize={font_size}",
            f"PrimaryColour={_ass_color(style.get('primary_color', '#FFFFFF'), '字幕颜色')}",
            f"OutlineColour={_ass_color(style.get('outline_color', '#000000'), '字幕描边颜色')}",
            f"Outline={outline_width:g}",
            f"Shadow={shadow:g}",
            f"Alignment={alignment}",
            f"MarginV={margin_v}",
            f"Bold={-1 if bold else 0}",
        ]
    )


def _filter_path(path: Path) -> str:
    value = str(path).replace("\\", "/")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError("字幕路径包含不允许的控制字符")
    for character in ("\\", ":", "'", ",", ";", "[", "]"):
        value = value.replace(character, "\\" + character)
    return value


class FfmpegTimelineRenderer:
    """Allowlist-only compiler from an internal timeline model to FFmpeg.

    This class never accepts raw filter expressions or shell fragments. Input
    and output roots can be pinned to the content object store, which prevents
    a public route from turning asset IDs into arbitrary filesystem access.
    """

    capabilities: ClassVar[dict[str, Any]] = {
        "split_model": "repeat-asset-with-source-ranges",
        "transitions": ["none", "cut", *_TRANSITIONS],
        "true_xfade": True,
        "fit_modes": sorted(_FIT_MODES),
        "source_audio": {"policies": sorted(_SOURCE_AUDIO_POLICIES), "per_clip_volume": True},
        "audio_tracks": ["voice", "music", "sfx"],
        "audio_controls": ["offset", "trim", "speed", "volume", "fade", "loop", "ducking"],
        "logo_intervals": True,
        "subtitles": sorted(_SUBTITLE_EXTENSIONS),
        "raw_filters": False,
        "public_paths": False,
        "overwrite_requires_opt_in": True,
    }

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        timeout_seconds: float = 600,
        allowed_input_roots: tuple[Path, ...] = (),
        allowed_output_roots: tuple[Path, ...] = (),
    ):
        if timeout_seconds <= 0:
            raise ValueError("FFmpeg 超时时间必须大于 0")
        self.ffmpeg = _ffmpeg_executable(ffmpeg_path)
        self.ffprobe = _ffprobe_executable(self.ffmpeg)
        self.timeout_seconds = timeout_seconds
        self.allowed_input_roots = tuple(
            path.expanduser().resolve() for path in allowed_input_roots
        )
        self.allowed_output_roots = tuple(
            path.expanduser().resolve() for path in allowed_output_roots
        )
        self.status = ProviderStatus(
            provider_id="local-ffmpeg-timeline",
            name="ffmpeg-timeline-renderer",
            capability="timeline-video-composition",
            mode="local",
            external=False,
            enabled=True,
            model=None,
        )

    def _input_path(self, value: Path) -> Path:
        if not isinstance(value, Path):
            raise TypeError("时间线内部素材路径必须是 pathlib.Path")
        path = value.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("时间线素材不是文件")
        if not _is_within(path, self.allowed_input_roots):
            raise ValueError("时间线素材超出内容服务对象存储目录")
        return path

    def _output_path(self, value: Path) -> Path:
        if not isinstance(value, Path):
            raise TypeError("时间线内部输出路径必须是 pathlib.Path")
        path = value.expanduser().resolve()
        if path.suffix.lower() != ".mp4":
            raise ValueError("时间线主渲染当前仅允许 MP4 输出")
        if not _is_within(path, self.allowed_output_roots):
            raise ValueError("时间线输出超出内容服务目录")
        return path

    def _resolve_clip(self, clip: TimelineClip) -> _ResolvedClip:
        _validate_asset_id(clip.asset_id)
        path = self._input_path(clip.source_path)
        if clip.kind not in {"video", "image"}:
            raise ValueError("时间线画面素材类型仅支持 video 或 image")
        if clip.kind == "image" and path.suffix.lower() not in _IMAGE_EXTENSIONS:
            raise ValueError("图片镜头的文件类型无效")
        if clip.kind == "video" and path.suffix.lower() not in _VIDEO_EXTENSIONS:
            raise ValueError("视频镜头的文件类型无效")
        if clip.source_in_seconds < 0:
            raise ValueError("镜头入点不能小于 0")
        if clip.kind == "image" and clip.source_in_seconds != 0:
            raise ValueError("图片镜头不支持非零入点")
        if (
            clip.source_out_seconds is not None
            and clip.source_out_seconds <= clip.source_in_seconds
        ):
            raise ValueError("镜头出点必须大于入点")
        if clip.duration_seconds is not None and clip.duration_seconds <= 0:
            raise ValueError("镜头时长必须大于 0")
        probed_duration, probed_audio = _probe(path, self.ffprobe, self.ffmpeg)
        ranged_duration = (
            clip.source_out_seconds - clip.source_in_seconds
            if clip.source_out_seconds is not None
            else None
        )
        if (
            ranged_duration is not None
            and clip.duration_seconds is not None
            and abs(ranged_duration - clip.duration_seconds) > 0.002
        ):
            raise ValueError("镜头 source_out 与 duration 表示的裁剪时长不一致")
        duration = clip.duration_seconds or ranged_duration
        if duration is None and probed_duration is not None:
            duration = probed_duration - clip.source_in_seconds
        if duration is None or duration <= 0:
            raise ValueError("镜头必须提供可验证的裁剪时长")
        # Still images are looped for their requested display duration. A probe
        # may report one frame (e.g. 0.04s), which is not a playback limit.
        if (
            clip.kind == "video"
            and probed_duration is not None
            and clip.source_in_seconds + duration > probed_duration + 0.05
        ):
            raise ValueError("镜头裁剪范围超过源素材时长")
        transition = clip.transition.strip().lower()
        if transition not in {"", "none", "cut", *_TRANSITIONS}:
            raise ValueError(f"不支持的视频转场：{clip.transition}")
        if not 0 <= clip.transition_duration_seconds <= 5:
            raise ValueError("转场时长必须在 0 到 5 秒之间")
        if clip.fit_mode not in _FIT_MODES:
            raise ValueError("镜头适配模式仅支持 contain 或 cover")
        if clip.source_audio not in _SOURCE_AUDIO_POLICIES:
            raise ValueError("源音轨策略仅支持 keep 或 mute")
        if not 0 <= clip.source_audio_volume <= 4:
            raise ValueError("源音轨音量必须在 0 到 4 之间")
        has_audio = clip.has_source_audio if clip.has_source_audio is not None else probed_audio
        if clip.kind == "image":
            has_audio = False
        if clip.source_audio == "keep" and has_audio is None:
            raise ValueError("保留源音轨时必须可探测音轨或显式提供 has_source_audio")
        return _ResolvedClip(
            value=replace(clip, transition=transition or "none"),
            path=path,
            duration=duration,
            has_audio=bool(has_audio),
        )

    def _resolve_audio(self, track: TimelineAudioTrack, total_duration: float) -> _ResolvedAudio:
        _validate_asset_id(track.asset_id)
        path = self._input_path(track.source_path)
        if track.kind not in _AUDIO_KINDS:
            raise ValueError("附加音轨类型仅支持 voice、music 或 sfx")
        if track.offset_seconds < 0 or track.source_in_seconds < 0:
            raise ValueError("音轨偏移和入点不能小于 0")
        if track.offset_seconds >= total_duration:
            raise ValueError("音轨偏移必须位于视频时间线内")
        if (
            track.source_out_seconds is not None
            and track.source_out_seconds <= track.source_in_seconds
        ):
            raise ValueError("音轨出点必须大于入点")
        if track.duration_seconds is not None and track.duration_seconds <= 0:
            raise ValueError("音轨时长必须大于 0")
        _atempo_filter_chain(track.speed)
        if not 0 <= track.volume <= 4:
            raise ValueError("音轨音量必须在 0 到 4 之间")
        if track.fade_in_seconds < 0 or track.fade_out_seconds < 0:
            raise ValueError("音轨淡入淡出不能小于 0")
        if path.suffix.lower() not in _AUDIO_EXTENSIONS:
            raise ValueError("附加音轨的文件类型无效")
        probed_duration, probed_audio = _probe(path, self.ffprobe)
        if probed_audio is False:
            raise ValueError("附加音轨素材不包含音频流")
        if track.loop_source_duration_seconds is not None:
            span = track.loop_source_duration_seconds
            if not track.loop or not math.isfinite(span) or span <= 0:
                raise ValueError("循环音频的裁剪范围无效")
            if (
                probed_duration is not None
                and track.source_in_seconds + span > probed_duration + 0.05
            ):
                raise ValueError("循环音频裁剪范围超过源素材时长")
        source_duration = (
            track.source_out_seconds - track.source_in_seconds
            if track.source_out_seconds is not None
            else (
                probed_duration - track.source_in_seconds if probed_duration is not None else None
            )
        )
        remaining = total_duration - track.offset_seconds
        if track.duration_seconds is not None:
            duration = min(track.duration_seconds, remaining)
        elif track.loop:
            duration = remaining
        elif source_duration is not None:
            duration = min(source_duration / track.speed, remaining)
        else:
            raise ValueError("音轨必须提供可验证的裁剪时长")
        consumed_source_duration = duration * track.speed
        if (
            not track.loop
            and source_duration is not None
            and consumed_source_duration > source_duration + 0.05
        ):
            raise ValueError("非循环音轨时长超过源素材裁剪范围")
        if (
            not track.loop
            and probed_duration is not None
            and track.source_in_seconds + consumed_source_duration > probed_duration + 0.05
        ):
            raise ValueError("音轨裁剪范围超过源素材时长")
        if track.fade_in_seconds > duration or track.fade_out_seconds > duration:
            raise ValueError("音轨淡入淡出不能超过音轨时长")
        return _ResolvedAudio(track, path, duration)

    @staticmethod
    def _validate_request(request: TimelineRenderRequest) -> None:
        if not request.clips:
            raise ValueError("时间线至少需要一个画面镜头")
        if not 64 <= request.width <= 7680 or not 64 <= request.height <= 4320:
            raise ValueError("视频宽高超出 64x64 到 7680x4320 的允许范围")
        if not 1 <= request.fps <= 120:
            raise ValueError("视频帧率必须在 1 到 120 之间")
        if request.duration_seconds is not None and (
            not math.isfinite(request.duration_seconds) or not 0 < request.duration_seconds <= 600
        ):
            raise ValueError("工程总时长必须大于 0 且不超过 600 秒")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", request.background_color):
            raise ValueError("背景颜色必须是 #RRGGBB")
        video_bitrate_match = re.fullmatch(
            r"([1-9][0-9]{0,2})([kKmM])",
            request.video_bitrate,
        )
        if not video_bitrate_match:
            raise ValueError("视频码率格式无效")
        video_bitrate = int(video_bitrate_match.group(1)) * (
            1000 if video_bitrate_match.group(2).lower() == "k" else 1_000_000
        )
        if not 100_000 <= video_bitrate <= 100_000_000:
            raise ValueError("视频码率必须在 100k 到 100M 之间")
        audio_bitrate_match = re.fullmatch(r"([1-9][0-9]{0,3})[kK]", request.audio_bitrate)
        if not audio_bitrate_match:
            raise ValueError("音频码率格式无效")
        if not 32 <= int(audio_bitrate_match.group(1)) <= 512:
            raise ValueError("音频码率必须在 32k 到 512k 之间")
        ducking = request.ducking
        unknown_targets = set(ducking.target_kinds).difference({"source", "voice", "music", "sfx"})
        if unknown_targets or "voice" in ducking.target_kinds:
            raise ValueError("Ducking 目标仅支持 source、music 或 sfx")
        if not 0.00097563 <= ducking.threshold <= 1:
            raise ValueError("Ducking threshold 超出 FFmpeg 安全范围")
        if not 1 <= ducking.ratio <= 20:
            raise ValueError("Ducking ratio 必须在 1 到 20 之间")
        if not 0.00001 <= ducking.attack_seconds <= 2:
            raise ValueError("Ducking attack 必须在 0.00001 到 2 秒之间")
        if not 0.00001 <= ducking.release_seconds <= 9:
            raise ValueError("Ducking release 必须在 0.00001 到 9 秒之间")

    @staticmethod
    def _transition_schedule(
        clips: list[_ResolvedClip],
    ) -> tuple[list[float], list[float], tuple[str, ...], float]:
        starts = [0.0]
        overlaps = [0.0]
        transitions: list[str] = []
        if clips[0].value.transition not in {"none", "cut"}:
            raise ValueError("第一个镜头不能设置入场转场")
        duration = clips[0].duration
        for index in range(1, len(clips)):
            current = clips[index]
            transition = current.value.transition
            if transition in {"none", "cut"}:
                overlap = 0.0
                transitions.append("none")
            else:
                overlap = min(
                    current.value.transition_duration_seconds,
                    clips[index - 1].duration / 2,
                    current.duration / 2,
                )
                if overlap <= 0:
                    raise ValueError("有效 xfade 转场时长必须大于 0")
                transitions.append(transition)
            starts.append(duration - overlap)
            overlaps.append(overlap)
            duration += current.duration - overlap
        return starts, overlaps, tuple(transitions), duration

    def _build_plan(self, request: TimelineRenderRequest) -> _RenderPlan:
        self._validate_request(request)
        output_path = self._output_path(request.output_path)
        clips = [self._resolve_clip(clip) for clip in request.clips]
        starts, overlaps, transitions, total_duration = self._transition_schedule(clips)
        total_duration = max(total_duration, request.duration_seconds or 0)
        audio_tracks = [
            self._resolve_audio(track, 600 if request.extend_to_audio else total_duration)
            for track in request.audio_tracks
        ]
        if request.extend_to_audio and audio_tracks:
            total_duration = max(
                total_duration,
                *(audio.value.offset_seconds + audio.duration for audio in audio_tracks),
            )

        logos: list[tuple[TimelineLogo, Path, float]] = []
        for logo in request.logos:
            _validate_asset_id(logo.asset_id)
            path = self._input_path(logo.source_path)
            if path.suffix.lower() not in _IMAGE_EXTENSIONS:
                raise ValueError("Logo 文件类型无效")
            end = (
                total_duration
                if logo.end_seconds is None
                else min(
                    logo.end_seconds,
                    total_duration,
                )
            )
            if logo.start_seconds < 0 or logo.start_seconds >= end:
                raise ValueError("Logo 显示区间无效")
            if not 1 <= logo.width <= 7680:
                raise ValueError("Logo 渲染宽度必须在 1 到 7680 之间")
            if logo.position not in _LOGO_POSITIONS:
                raise ValueError("Logo 位置无效")
            if not 0 <= logo.opacity <= 1:
                raise ValueError("Logo 透明度必须在 0 到 1 之间")
            if (logo.x_percent is None) != (logo.y_percent is None):
                raise ValueError("Logo 自定义位置必须同时提供 X 和 Y")
            if logo.x_percent is not None and not 0 <= logo.x_percent <= 100:
                raise ValueError("Logo X 位置必须在 0 到 100 之间")
            if logo.y_percent is not None and not 0 <= logo.y_percent <= 100:
                raise ValueError("Logo Y 位置必须在 0 到 100 之间")
            logos.append((logo, path, end))

        subtitles: list[tuple[TimelineSubtitle, Path, str]] = []
        for subtitle in request.subtitles:
            _validate_asset_id(subtitle.asset_id)
            path = self._input_path(subtitle.source_path)
            if path.suffix.lower() not in _SUBTITLE_EXTENSIONS:
                raise ValueError("字幕文件仅支持 SRT、ASS、SSA 或 VTT")
            style = _subtitle_force_style(
                subtitle.style,
                subtitle.canvas_width,
                subtitle.canvas_height,
            )
            subtitles.append((subtitle, path, style))

        input_paths = {
            *(clip.path for clip in clips),
            *(audio.path for audio in audio_tracks),
            *(path for _, path, _ in logos),
            *(path for _, path, _ in subtitles),
        }
        if output_path in input_paths:
            raise ValueError("时间线输出不能覆盖输入素材")
        if output_path.exists() and not request.overwrite:
            raise ValueError("时间线输出已存在；覆盖必须由服务端显式启用")

        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        input_indices: dict[tuple[str, int], int] = {}
        next_input = 0
        for index, clip in enumerate(clips):
            input_indices[("clip", index)] = next_input
            next_input += 1
            if clip.value.kind == "image":
                command.extend(
                    [
                        "-loop",
                        "1",
                        "-framerate",
                        str(request.fps),
                        "-t",
                        _format_seconds(clip.duration),
                    ]
                )
            command.extend(["-protocol_whitelist", "file,pipe", "-i", str(clip.path)])
        for index, audio in enumerate(audio_tracks):
            input_indices[("audio", index)] = next_input
            next_input += 1
            if audio.value.loop and audio.value.loop_source_duration_seconds is None:
                command.extend(["-stream_loop", "-1"])
            command.extend(["-protocol_whitelist", "file,pipe", "-i", str(audio.path)])
        for index, (_, path, _) in enumerate(logos):
            input_indices[("logo", index)] = next_input
            next_input += 1
            command.extend(
                [
                    "-loop",
                    "1",
                    "-framerate",
                    str(request.fps),
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(path),
                ]
            )

        filters: list[str] = []
        for index, clip in enumerate(clips):
            input_index = input_indices[("clip", index)]
            if clip.value.fit_mode == "cover":
                resize = (
                    f"scale={request.width}:{request.height}:force_original_aspect_ratio=increase,"
                    f"crop={request.width}:{request.height}"
                )
            else:
                color = "0x" + request.background_color[1:]
                resize = (
                    f"scale={request.width}:{request.height}:force_original_aspect_ratio=decrease,"
                    f"pad={request.width}:{request.height}:(ow-iw)/2:(oh-ih)/2:color={color}"
                )
            source_in = 0 if clip.value.kind == "image" else clip.value.source_in_seconds
            filters.append(
                f"[{input_index}:v]{resize},setsar=1,"
                f"trim=start={_format_seconds(source_in)}:duration={_format_seconds(clip.duration)},"
                f"setpts=PTS-STARTPTS,fps={request.fps},format=yuv420p,"
                f"settb=1/{request.fps}"
                f"[v{index}]"
            )

        current_video = "v0"
        current_duration = clips[0].duration
        for index in range(1, len(clips)):
            output = f"vjoin{index}"
            overlap = overlaps[index]
            if overlap:
                offset = current_duration - overlap
                filters.append(
                    f"[{current_video}][v{index}]xfade="
                    f"transition={_TRANSITIONS[clips[index].value.transition]}:"
                    f"duration={_format_seconds(overlap)}:offset={_format_seconds(offset)}"
                    f"[{output}]"
                )
            else:
                # concat resets its output time base. Normalize it before a
                # later xfade joins this stream with a frame-based clip.
                filters.append(
                    f"[{current_video}][v{index}]concat=n=2:v=1:a=0,"
                    f"fps={request.fps},settb=1/{request.fps}[{output}]"
                )
            current_video = output
            current_duration += clips[index].duration - overlap

        if total_duration > current_duration + 0.000001:
            filters.append(
                f"[{current_video}]tpad=stop_mode=clone:"
                f"stop_duration={_format_seconds(total_duration - current_duration)},"
                f"trim=duration={_format_seconds(total_duration)}[vextended]"
            )
            current_video = "vextended"

        for index, (_, path, force_style) in enumerate(subtitles):
            output = f"vsubtitle{index}"
            style = f":force_style='{force_style}'" if force_style else ""
            filters.append(
                f"[{current_video}]subtitles=filename='{_filter_path(path)}'{style}[{output}]"
            )
            current_video = output

        for index, (logo, _, end) in enumerate(logos):
            input_index = input_indices[("logo", index)]
            logo_label = f"logo{index}"
            output = f"vlogo{index}"
            filters.append(
                f"[{input_index}:v]format=rgba,scale={logo.width}:-1,"
                f"colorchannelmixer=aa={logo.opacity:.3f}[{logo_label}]"
            )
            overlay_position = _LOGO_POSITIONS[logo.position]
            if logo.x_percent is not None and logo.y_percent is not None:
                x_ratio = logo.x_percent / 100
                y_ratio = logo.y_percent / 100
                overlay_position = (
                    f"main_w*{x_ratio:.6f}-overlay_w/2:main_h*{y_ratio:.6f}-overlay_h/2"
                )
            filters.append(
                f"[{current_video}][{logo_label}]overlay={overlay_position}:"
                f"eof_action=repeat:enable='between(t,{_format_seconds(logo.start_seconds)},"
                f"{_format_seconds(end)})'[{output}]"
            )
            current_video = output

        audio_entries: list[tuple[str, TimelineAudioKind]] = []
        source_audio_count = 0
        for index, clip in enumerate(clips):
            if clip.value.source_audio != "keep" or not clip.has_audio:
                continue
            source_audio_count += 1
            input_index = input_indices[("clip", index)]
            label = f"asource{index}"
            parts = [
                (
                    f"[{input_index}:a]atrim="
                    f"start={_format_seconds(clip.value.source_in_seconds)}:"
                    f"duration={_format_seconds(clip.duration)}"
                ),
                "asetpts=PTS-STARTPTS",
                "aresample=48000",
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
                f"apad=pad_dur={_format_seconds(clip.duration)}",
                f"atrim=duration={_format_seconds(clip.duration)}",
                f"volume={clip.value.source_audio_volume:.4f}",
            ]
            if overlaps[index]:
                parts.append(f"afade=t=in:st=0:d={_format_seconds(overlaps[index])}")
            if index + 1 < len(clips) and overlaps[index + 1]:
                fade = overlaps[index + 1]
                parts.append(
                    f"afade=t=out:st={_format_seconds(clip.duration - fade)}:"
                    f"d={_format_seconds(fade)}"
                )
            delay = round(starts[index] * 1000)
            parts.extend(
                [
                    f"adelay={delay}:all=1",
                    f"apad=pad_dur={_format_seconds(total_duration)}",
                    f"atrim=duration={_format_seconds(total_duration)}[{label}]",
                ]
            )
            filters.append(",".join(parts))
            audio_entries.append((label, "source"))

        for index, audio in enumerate(audio_tracks):
            input_index = input_indices[("audio", index)]
            track = audio.value
            label = f"atrack{index}"
            parts = [
                (
                    f"[{input_index}:a]atrim="
                    f"start={_format_seconds(track.source_in_seconds)}:"
                    f"duration={_format_seconds(track.loop_source_duration_seconds or audio.duration * track.speed)}"
                ),
                "asetpts=PTS-STARTPTS",
                "aresample=48000",
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
            ]
            if track.loop_source_duration_seconds is not None:
                samples = max(1, round(track.loop_source_duration_seconds * 48_000))
                parts.append(f"aloop=loop=-1:size={samples}")
            parts.extend(_atempo_filter_chain(track.speed))
            parts.extend(
                [
                    f"apad=pad_dur={_format_seconds(audio.duration)}",
                    f"atrim=duration={_format_seconds(audio.duration)}",
                    f"volume={track.volume:.4f}",
                ]
            )
            if track.fade_in_seconds:
                parts.append(f"afade=t=in:st=0:d={_format_seconds(track.fade_in_seconds)}")
            if track.fade_out_seconds:
                parts.append(
                    f"afade=t=out:st={_format_seconds(audio.duration - track.fade_out_seconds)}:"
                    f"d={_format_seconds(track.fade_out_seconds)}"
                )
            delay = round(track.offset_seconds * 1000)
            parts.extend(
                [
                    f"adelay={delay}:all=1",
                    f"apad=pad_dur={_format_seconds(total_duration)}",
                    f"atrim=duration={_format_seconds(total_duration)}[{label}]",
                ]
            )
            filters.append(",".join(parts))
            audio_entries.append((label, track.kind))

        ducking_applied = False
        if request.ducking.enabled:
            voice_entries = [entry for entry in audio_entries if entry[1] == "voice"]
            duck_entries = [
                entry for entry in audio_entries if entry[1] in request.ducking.target_kinds
            ]
            if not voice_entries:
                raise ValueError("启用 Ducking 时必须至少有一条 voice 音轨")
            if not duck_entries:
                raise ValueError("启用 Ducking 时必须至少有一条目标音轨")
            if len(voice_entries) == 1:
                voice_bus = voice_entries[0][0]
            else:
                voice_bus = "voicebus"
                inputs = "".join(f"[{label}]" for label, _ in voice_entries)
                filters.append(
                    f"{inputs}amix=inputs={len(voice_entries)}:duration=longest:"
                    f"dropout_transition=0:normalize=0[{voice_bus}]"
                )
            split_labels = [
                "voicefinal",
                *(f"sidechain{index}" for index in range(len(duck_entries))),
            ]
            filters.append(
                f"[{voice_bus}]asplit=outputs={len(split_labels)}"
                + "".join(f"[{label}]" for label in split_labels)
            )
            replacements: dict[str, str] = {}
            for index, (label, _) in enumerate(duck_entries):
                output = f"ducked{index}"
                filters.append(
                    f"[{label}][sidechain{index}]sidechaincompress="
                    f"threshold={request.ducking.threshold:.6f}:"
                    f"ratio={request.ducking.ratio:.3f}:"
                    f"attack={request.ducking.attack_seconds * 1000:.3f}:"
                    f"release={request.ducking.release_seconds * 1000:.3f}"
                    f"[{output}]"
                )
                replacements[label] = output
            audio_entries = [
                (replacements.get(label, label), kind)
                for label, kind in audio_entries
                if kind != "voice"
            ]
            audio_entries.append(("voicefinal", "voice"))
            ducking_applied = True

        audio_label: str | None = None
        if audio_entries:
            if len(audio_entries) == 1:
                filters.append(
                    f"[{audio_entries[0][0]}]atrim=duration={_format_seconds(total_duration)},"
                    "alimiter=limit=0.95[amixed]"
                )
            else:
                inputs = "".join(f"[{label}]" for label, _ in audio_entries)
                filters.append(
                    f"{inputs}amix=inputs={len(audio_entries)}:duration=longest:"
                    f"dropout_transition=0:normalize=0,"
                    f"atrim=duration={_format_seconds(total_duration)},"
                    "alimiter=limit=0.95[amixed]"
                )
            audio_label = "amixed"

        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{current_video}]"])
        if audio_label:
            command.extend(
                [
                    "-map",
                    f"[{audio_label}]",
                    "-c:a",
                    "aac",
                    "-b:a",
                    request.audio_bitrate.lower(),
                    "-ar",
                    "48000",
                ]
            )
        else:
            command.append("-an")
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                request.video_bitrate.lower(),
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(request.fps),
                "-t",
                _format_seconds(total_duration),
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        return _RenderPlan(
            command=tuple(command),
            output_path=output_path,
            duration=total_duration,
            transitions=transitions,
            source_audio_count=source_audio_count,
            ducking_applied=ducking_applied,
        )

    def build_command(self, request: TimelineRenderRequest) -> tuple[str, ...]:
        """Validate and compile a request; useful for job queues and dry runs."""

        return self._build_plan(request).command

    def render(self, request: TimelineRenderRequest) -> TimelineRenderResult:
        plan = self._build_plan(request)
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                list(plan.command),
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("FFmpeg 时间线渲染超时") from exc
        except subprocess.CalledProcessError as exc:
            stderr = " ".join((exc.stderr or "").split())[-1600:]
            raise RuntimeError(f"FFmpeg 时间线渲染失败：{stderr}") from exc
        if not plan.output_path.is_file() or plan.output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg 未生成有效时间线视频")
        return TimelineRenderResult(
            output_path=plan.output_path,
            duration_seconds=plan.duration,
            width=request.width,
            height=request.height,
            fps=request.fps,
            clip_count=len(request.clips),
            audio_track_count=len(request.audio_tracks),
            transitions_applied=plan.transitions,
            source_audio_clip_count=plan.source_audio_count,
            ducking_applied=plan.ducking_applied,
            logos_applied=len(request.logos),
            subtitles_burned=len(request.subtitles),
        )
