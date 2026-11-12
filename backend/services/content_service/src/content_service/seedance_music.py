"""Instrumental BGM via a dedicated Seedance request and local audio extraction.

This adapter does not share video-editor jobs, references, or timeline state.
Only the Ark credential is shared; the music model and limits are independent.
"""

from __future__ import annotations

import logging
import math
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .hiagent_adapters import HiAgentClient, HiAgentVideoGenerationPort
from .hiagent_network import configured_hiagent_base_url
from .llm_adapters import ProviderCallError
from .media_adapters import (
    VolcengineArkAiVideoGenerationPort,
    _bounded_response_bytes,
    _ffmpeg_executable,
    _safe_supplier_audio_url,
    _validate_audio_content,
)
from .providers import (
    AiVideoGenerationRequest,
    MusicGenerationRequest,
    MusicGenerationResult,
    ProviderStatus,
)

DEFAULT_SEEDANCE_MUSIC_MODEL = "doubao-seedance-2-5-260628"
_logger = logging.getLogger(__name__)


def _error(message: str, code: str = "seedance_music_failed") -> ProviderCallError:
    return ProviderCallError(message, code=code, status_code=502, retryable=False)


def _provider_error(exc: ProviderCallError) -> ProviderCallError:
    # Use raw details for classification only; never show URLs, headers or English
    # provider response bodies to the user.
    detail = f"{exc.code} {exc}".lower()
    if exc.status_code == 401:
        message = "方舟访问密钥校验失败，请检查背景音乐使用的密钥。"
    elif exc.status_code in {403, 404} or any(
        word in detail for word in ("notopen", "permission", "notfound", "accessdenied")
    ):
        message = (
            "当前方舟账号无法调用背景音乐所选的 Seedance 模型，请检查该模型的开通状态和调用权限。"
        )
    elif any(word in detail for word in ("balance", "quota", "arrears")):
        message = "方舟服务额度不足，请检查账号余额和模型额度后再生成背景音乐。"
    elif exc.status_code == 429:
        message = "背景音乐生成请求较多，请稍后再试。"
    elif any(word in detail for word in ("sensitive", "safety", "contentpolicy", "blocked")):
        message = "背景音乐描述未通过模型审核，请调整描述后再生成。"
    elif exc.status_code == 400:
        message = "当前 Seedance 模型未接受背景音乐生成参数，请检查模型配置和所选时长。"
    else:
        message = "Seedance 背景音乐服务暂时无法完成请求，请稍后再试。"
    return _error(message)


class SeedanceMusicGenerationPort:
    def __init__(
        self,
        *,
        provider_id: str = "volcengine-ark-seedance-music",
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        api_key: str | None = None,
        model: str = DEFAULT_SEEDANCE_MUSIC_MODEL,
        timeout_seconds: float = 480,
        poll_interval_seconds: float = 5,
        minimum_duration_seconds: float = 5,
        maximum_duration_seconds: float = 30,
        max_bytes: int = 64 * 1024 * 1024,
        allowed_download_hosts: tuple[str, ...] = (),
        ffmpeg_path: str | None = None,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "ark.cn-beijing.volces.com"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Seedance 背景音乐必须使用方舟官方 HTTPS 接口")
        model_max_seconds = 15 if "seedance-2-0-" in model else 30
        if not 5 <= minimum_duration_seconds <= maximum_duration_seconds <= model_max_seconds:
            raise ValueError(
                f"当前 Seedance 背景音乐模型的上游生成时长必须在 5 到 {model_max_seconds} 秒内"
            )
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
            or max_bytes < 1024
        ):
            raise ValueError("背景音乐等待时间或文件大小配置无效")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.minimum_duration_seconds = math.ceil(minimum_duration_seconds)
        self.maximum_duration_seconds = math.floor(maximum_duration_seconds)
        self.max_bytes = max_bytes
        self.ffmpeg_path = ffmpeg_path
        self.allowed_download_hosts = tuple(
            value.strip().lower().rstrip(".")
            for value in (allowed_download_hosts or ("volces.com", "volccdn.com"))
            if value.strip()
        )
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="Seedance 背景音乐",
            capability="music-generation",
            mode="api",
            external=True,
            enabled=True,
            model=model,
        )
        self.capabilities = {
            "instrumental_only": True,
            "sound_effects": False,
            "formats": ["mp3", "wav"],
            "duration_seconds": {
                "minimum": 1,
                "maximum": self.maximum_duration_seconds,
                "provider_minimum": self.minimum_duration_seconds,
                "control": "video-audio-extraction-with-trim",
            },
            "returns_bytes": True,
            "temporary_urls_persisted_immediately": True,
            "parameter_source": "seedance-audio-video-generation",
            "billing_basis": "video-generation",
            "audio_only_api": False,
        }
        # A separate provider instance is essential: never switch the editor's
        # video model or reuse its in-flight jobs when generating music.
        self.video_provider = VolcengineArkAiVideoGenerationPort(
            provider_id=provider_id,
            base_url=base_url,
            api_key=api_key or "",
            model=model,
            timeout_seconds=min(timeout_seconds, 30),
            supported_durations=tuple(
                range(self.minimum_duration_seconds, self.maximum_duration_seconds + 1)
            ),
            supported_ratios=("16:9",),
            supported_resolutions=("480p",),
        )

    def _download(self, url: str) -> bytes:
        try:
            safe_url = _safe_supplier_audio_url(url, "背景音乐", self.allowed_download_hosts)
            # Do not forward the Ark key to the signed media URL.
            with (
                httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client,
                client.stream("GET", safe_url) as response,
            ):
                if response.is_redirect:
                    raise _error("背景音乐下载地址发生跳转，已停止下载，请稍后再试。")
                response.raise_for_status()
                return _bounded_response_bytes(
                    response, max_bytes=self.max_bytes, provider_id="背景音乐"
                )
        except httpx.HTTPError as exc:
            raise _error("配乐已生成，但下载失败，请稍后再试。") from exc

    def _extract(self, video: bytes, request: MusicGenerationRequest) -> tuple[bytes, float]:
        ffmpeg = _ffmpeg_executable(self.ffmpeg_path)
        duration = float(request.duration_seconds)
        fade = min(0.2, duration / 5)
        with tempfile.TemporaryDirectory(prefix="content-seedance-bgm-") as directory:
            source = Path(directory) / "source.mp4"
            decoded = Path(directory) / "music.wav"
            source.write_bytes(video)
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            try:
                subprocess.run(
                    [
                        *command,
                        "-i",
                        str(source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-t",
                        str(duration),
                        "-af",
                        f"afade=t=out:st={duration - fade:g}:d={fade:g}",
                        "-ar",
                        "44100",
                        "-ac",
                        "2",
                        "-c:a",
                        "pcm_s16le",
                        "-fs",
                        str(self.max_bytes + 1),
                        str(decoded),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                # Check actual decoded samples, not supplier-reported duration.
                with wave.open(str(decoded), "rb") as stream:
                    frame_count = stream.getnframes()
                    frame_size = stream.getnchannels() * stream.getsampwidth()
                    if frame_count * frame_size > self.max_bytes:
                        raise _error("生成的背景音乐文件过大，请缩短时长后再试。")
                    samples = stream.readframes(frame_count)
                    actual_duration = len(samples) / frame_size / stream.getframerate()
                    if (
                        len(samples) != frame_count * frame_size
                        or abs(actual_duration - duration) > 0.05
                    ):
                        raise _error("模型返回的声音不足所选时长，无法导出完整配乐，请重新生成。")
                output = decoded
                if request.audio_format == "mp3":
                    output = Path(directory) / "music.mp3"
                    subprocess.run(
                        [
                            *command,
                            "-i",
                            str(decoded),
                            "-c:a",
                            "libmp3lame",
                            "-b:a",
                            "256k",
                            "-fs",
                            str(self.max_bytes + 1),
                            str(output),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=60,
                    )
                audio = output.read_bytes()
            except (OSError, RuntimeError, subprocess.SubprocessError, wave.Error, EOFError) as exc:
                if isinstance(exc, ProviderCallError):
                    raise
                raise _error(
                    "模型返回的视频没有可用音轨，或音轨提取失败，未生成背景音乐文件。请重新生成；如仍失败，请联系管理员。"
                ) from exc
        _validate_audio_content(audio, request.audio_format, "背景音乐", max_bytes=self.max_bytes)
        return audio, actual_duration

    def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        if not self.api_key:
            raise _error("背景音乐尚未配置方舟访问密钥，请联系管理员检查配置。")
        prompt = request.prompt.strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("请填写 1 到 2000 字的背景音乐描述")
        duration = request.duration_seconds
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or int(duration) != duration
            or not 1 <= duration <= self.maximum_duration_seconds
        ):
            raise ValueError(
                f"背景音乐时长请填写 1 到 {self.maximum_duration_seconds} 秒之间的整数"
            )
        if request.audio_format not in {"mp3", "wav"}:
            raise ValueError("背景音乐格式仅支持 MP3 或 WAV")
        # Fail before creating a paid remote task if local extraction is unavailable.
        try:
            _ffmpeg_executable(self.ffmpeg_path)
        except (OSError, RuntimeError, ImportError) as exc:
            raise _error(
                "背景音乐的音轨提取组件未就绪，请联系管理员检查；本次尚未提交模型生成。"
            ) from exc
        provider_duration = max(int(duration), self.minimum_duration_seconds)
        provider_prompt = (
            "制作一段以原创纯器乐背景音乐为核心的有声视频。"
            "声音必须是连续、清晰的音乐，不要人声、对白、旁白、歌词、哼唱、环境声或动作音效。"
            "画面保持静态纯色，无人物、文字或叙事；不得根据画面添加额外声音。"
            f"音乐应覆盖完整的 {provider_duration} 秒，节奏自然，结尾平稳收束。\n"
            f"用户的音乐风格要求：{prompt}"
        )
        deadline = time.monotonic() + self.timeout_seconds
        task_id = None
        try:
            task = self.video_provider.submit(
                AiVideoGenerationRequest(
                    prompt=provider_prompt,
                    duration_seconds=provider_duration,
                    ratio="16:9",
                    resolution="480p",
                    generate_audio=True,
                    idempotency_key=uuid4().hex,
                )
            )
            task_id = task.provider_task_id
            if not task_id:
                raise _error("背景音乐模型没有返回有效任务编号，请稍后再试。")
            _logger.info("Seedance music submitted provider_task_id=%s", task_id)
            while task.status in {"queued", "running"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _error(
                        "背景音乐生成等待超时。再次点击会提交新任务并可能重复计费，请先联系管理员查询本次结果。",
                        "music_timeout",
                    )
                time.sleep(min(self.poll_interval_seconds, remaining))
                if time.monotonic() >= deadline:
                    continue
                task = self.video_provider.get(task_id)
                if task.provider_task_id != task_id:
                    raise _error("背景音乐服务返回了不匹配的任务结果，已停止导出。")
            if task.status != "succeeded":
                raise ProviderCallError(
                    task.error_message or "生成未完成", code=task.error_code or "music_task_failed"
                )
            if not task.video_url:
                raise _error("模型已完成任务，但没有返回带配乐的视频文件，请稍后再试。")
        except ProviderCallError as exc:
            _logger.warning(
                "Seedance music task incomplete provider_task_id=%s code=%s", task_id, exc.code
            )
            if exc.code in {"seedance_music_failed", "music_timeout"}:
                raise
            raise _provider_error(exc) from exc
        audio, actual_duration = self._extract(self._download(task.video_url), request)
        return MusicGenerationResult(
            audio=audio,
            media_type="audio/mpeg" if request.audio_format == "mp3" else "audio/wav",
            extension=request.audio_format,
            duration_seconds=actual_duration,
            requested_duration_seconds=duration,
            provider_id=self.status.provider_id,
            model=self.model,
            metadata={
                "provider_task_id": task_id,
                "generation_method": "video-audio-extraction",
                "provider_duration_seconds": provider_duration,
                "billing_basis": "video-generation",
                "generate_audio": True,
                "trimmed": duration < provider_duration,
            },
        )


class HiAgentSeedanceMusicGenerationPort(SeedanceMusicGenerationPort):
    """Use HiAgent Seedance for the isolated music-to-video/audio pipeline."""

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        workspace_id: str,
        pro_model: str,
        fast_model: str,
        gateway_base_url: str = "",
        **kwargs,
    ):
        kwargs.pop("model", None)
        super().__init__(api_key="configured", model=pro_model, **kwargs)
        endpoint = urlsplit(gateway_base_url or configured_hiagent_base_url())
        client = HiAgentClient(
            access_key=access_key,
            secret_key=secret_key,
            workspace_id=workspace_id,
            host=endpoint.hostname,
            port=endpoint.port or 443,
        )
        self.video_provider = HiAgentVideoGenerationPort(
            client=client,
            pro_model=pro_model,
            fast_model=fast_model,
            provider_id=self.status.provider_id,
            durations=tuple(
                range(self.minimum_duration_seconds, self.maximum_duration_seconds + 1)
            ),
            ratios=("16:9",),
        )
        self.model = self.video_provider.status.model
        self.status = ProviderStatus(
            self.status.provider_id,
            "HiAgent Seedance 背景音乐",
            "music-generation",
            "api",
            True,
            True,
            self.model,
        )
        self.allowed_download_hosts = tuple(set(self.allowed_download_hosts) | {endpoint.hostname})
