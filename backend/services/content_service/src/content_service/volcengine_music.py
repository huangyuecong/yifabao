"""Volcengine Imagination instrumental BGM; never substitute TTS or mock audio.

Contract and HMAC signing reference (Apache-2.0, Volcano Engine copyright):
https://github.com/bytedance/agentkit-samples/blob/main/skills/
byted-music-generate/scripts/music_generate.py
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import subprocess
import tempfile
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .llm_adapters import ProviderCallError
from .media_adapters import (
    _bounded_response_bytes,
    _ffmpeg_executable,
    _safe_supplier_audio_url,
    _validate_audio_content,
)
from .providers import MusicGenerationRequest, MusicGenerationResult, ProviderStatus

_HOST = "open.volcengineapi.com"
_VERSION = "2024-08-12"
_REGION = "cn-beijing"
_SERVICE = "imagination"
_DOWNLOAD_HOSTS = ("volces.com", "volccdn.com", "byteimg.com", "bytecdn.cn")


def _music_error(message: str, code: str = "music_generation_failed") -> ProviderCallError:
    return ProviderCallError(message, code=code, retryable=True, status_code=502)


def _supplier_error(code: object, *, status: int = 400) -> ProviderCallError:
    # Never expose raw response text: it can contain prompt data or signed URLs.
    value = str(code or "").lower()
    if any(word in value for word in ("signature", "invalidaccess", "invalidtoken", "authfail")):
        return _music_error(
            "背景音乐服务的访问密钥校验失败，请联系管理员检查音乐服务密钥，修正后可重试。"
        )
    if any(word in value for word in ("quota", "balance", "expired", "arrears")):
        return _music_error(
            "背景音乐服务额度不足或已到期，请联系管理员检查服务额度，恢复后可重试。"
        )
    if status in {401, 403} or any(
        word in value for word in ("forbidden", "denied", "notopen", "notactivate")
    ):
        return _music_error(
            "背景音乐服务尚未开通或当前账号没有调用权限，请联系管理员检查开通状态和授权，处理后可重试。"
        )
    if status == 429 or any(word in value for word in ("throttl", "limitexceed", "ratelimit")):
        return _music_error("背景音乐服务当前请求较多，请稍后重试。")
    if any(word in value for word in ("sensitive", "content", "safety", "illegaltext")):
        return _music_error("背景音乐描述未通过服务审核，请调整描述后重新生成。")
    if status >= 500:
        return _music_error("背景音乐服务暂时无法处理请求，请稍后重试。")
    return _music_error(
        "背景音乐服务未能完成这次生成，请检查描述和时长后重试；如仍失败，请联系管理员。"
    )


def signed_music_headers(
    access_key: str, secret_key: str, action: str, body: bytes, *, now: datetime | None = None
) -> dict[str, str]:
    """Sign the exact serialized bytes sent to the official Imagination host."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json; charset=utf-8",
        "host": _HOST,
        "x-content-sha256": digest,
        "x-date": stamp,
    }
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    query = urlencode({"Action": action, "Version": _VERSION})
    canonical = f"POST\n/\n{query}\n{canonical_headers}\n{signed}\n{digest}"
    scope = f"{stamp[:8]}/{_REGION}/{_SERVICE}/request"
    to_sign = f"HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = secret_key.encode()
    for part in (stamp[:8], _REGION, _SERVICE, "request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers


class VolcengineMusicGenerationPort:
    def __init__(
        self,
        *,
        provider_id: str = "volcengine-bgm",
        base_url: str = "https://open.volcengineapi.com",
        access_key: str | None = None,
        secret_key: str | None = None,
        model: str = "v5.0",
        billing: str = "prepaid",
        timeout_seconds: float = 300,
        poll_interval_seconds: float = 10,
        max_bytes: int = 64 * 1024 * 1024,
        minimum_duration_seconds: float = 30,
        maximum_duration_seconds: float = 120,
        allowed_download_hosts: tuple[str, ...] = (),
    ):
        # Do not send cloud signing credentials to arbitrary configured hosts.
        if base_url.rstrip("/") != f"https://{_HOST}":
            raise ValueError("火山背景音乐必须使用官方 HTTPS 接口地址")
        if billing not in {"prepaid", "postpaid"}:
            raise ValueError("背景音乐计费接口配置无效")
        if not 30 <= minimum_duration_seconds <= maximum_duration_seconds <= 120:
            raise ValueError("火山背景音乐时长范围必须在 30 到 120 秒内")
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
            or max_bytes < 1024
        ):
            raise ValueError("背景音乐服务等待时间或文件大小配置无效")
        self.access_key = access_key
        self.secret_key = secret_key
        self.model = model
        self.billing = billing
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_bytes = max_bytes
        self.minimum_duration_seconds = minimum_duration_seconds
        self.maximum_duration_seconds = maximum_duration_seconds
        self.allowed_download_hosts = tuple(
            host.strip().lower().rstrip(".")
            for host in (allowed_download_hosts or _DOWNLOAD_HOSTS)
            if host.strip()
        )
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="火山背景音乐",
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
                "maximum": maximum_duration_seconds,
                "provider_minimum": minimum_duration_seconds,
                "control": "provider-duration-with-trim",
            },
            "returns_bytes": True,
            "temporary_urls_persisted_immediately": True,
            "parameter_source": "volcengine-imagination-genbgm",
        }

    def _request(self, client: httpx.Client, action: str, payload: dict, deadline: float) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _music_error(
                "背景音乐仍在生成，等待已超时。请稍后重试；再次生成会提交新的音乐任务。",
                "music_timeout",
            )
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = signed_music_headers(self.access_key or "", self.secret_key or "", action, body)
        url = f"https://{_HOST}/?{urlencode({'Action': action, 'Version': _VERSION})}"
        with client.stream(
            "POST", url, headers=headers, content=body, timeout=min(30, remaining)
        ) as response:
            raw = _bounded_response_bytes(
                response, max_bytes=1024 * 1024, provider_id="背景音乐服务"
            )
            try:
                data = json.loads(raw)
            except (ValueError, TypeError) as exc:
                raise _music_error("背景音乐服务返回的数据不完整，请稍后重试。") from exc
            if not isinstance(data, dict):
                raise _music_error("背景音乐服务返回的数据格式有误，请稍后重试。")
            metadata = data.get("ResponseMetadata") or {}
            error = metadata.get("Error") if isinstance(metadata, dict) else None
            if response.status_code >= 400 or error or data.get("Code", 0) not in (0, "0"):
                code = error.get("Code") if isinstance(error, dict) else data.get("Code")
                raise _supplier_error(code, status=response.status_code)
            result = data.get("Result")
            if not isinstance(result, dict):
                raise _music_error("背景音乐服务没有返回有效结果，请稍后重试。")
            return result

    def _download(self, client: httpx.Client, url: str, deadline: float) -> bytes:
        try:
            safe_url = _safe_supplier_audio_url(url, "背景音乐服务", self.allowed_download_hosts)
        except ProviderCallError as exc:
            raise _music_error(
                "背景音乐下载地址未通过安全检查，请联系管理员检查音乐下载域名配置。"
            ) from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _music_error("背景音乐已生成，但下载等待超时，请稍后重试。", "music_timeout")
        # No signing or Authorization headers are forwarded to a media CDN.
        with client.stream("GET", safe_url, timeout=min(60, remaining)) as response:
            if response.is_redirect:
                raise _music_error("背景音乐下载地址发生跳转，已停止下载，请稍后重试。")
            response.raise_for_status()
            return _bounded_response_bytes(
                response, max_bytes=self.max_bytes, provider_id="背景音乐服务"
            )

    def _convert(
        self, audio: bytes, requested_format: str, *, trim_duration_seconds: float | None = None
    ) -> bytes:
        source_format = "wav" if audio[:4] == b"RIFF" else "mp3"
        _validate_audio_content(audio, source_format, "背景音乐服务", max_bytes=self.max_bytes)
        if source_format == requested_format and trim_duration_seconds is None:
            return audio
        # GenBGM does not document a format selector. Convert actual returned media
        # locally instead of sending an invented upstream format parameter.
        with tempfile.TemporaryDirectory(prefix="content-volc-bgm-") as directory:
            source = Path(directory) / f"source.{source_format}"
            output = Path(directory) / f"music.{requested_format}"
            source.write_bytes(audio)
            codec = (
                ["-c:a", "pcm_s16le"]
                if requested_format == "wav"
                else ["-c:a", "libmp3lame", "-b:a", "256k"]
            )
            filters = []
            if trim_duration_seconds is not None:
                # Trim real samples only: never loop or pad a short supplier result.
                fade_seconds = min(0.2, trim_duration_seconds / 5)
                filters = [
                    "-af",
                    (
                        f"atrim=end={trim_duration_seconds:g},asetpts=PTS-STARTPTS,"
                        f"afade=t=out:st={trim_duration_seconds - fade_seconds:g}:d={fade_seconds:g}"
                    ),
                ]
            try:
                subprocess.run(
                    [
                        _ffmpeg_executable(),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-y",
                        "-i",
                        str(source),
                        "-vn",
                        *filters,
                        *codec,
                        "-fs",
                        str(self.max_bytes + 1),
                        str(output),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
                converted = output.read_bytes()
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                message = (
                    "背景音乐已生成，但短配乐裁切失败，请稍后重试；如仍失败，请联系管理员。"
                    if trim_duration_seconds is not None
                    else "背景音乐已生成，但音频格式转换失败，请换用另一种格式后重试。"
                )
                raise _music_error(message) from exc
        _validate_audio_content(
            converted, requested_format, "背景音乐服务", max_bytes=self.max_bytes
        )
        return converted

    def _decoded_duration(self, audio: bytes) -> float:
        # Decode MP3 to account for codec delay/padding instead of trusting metadata.
        decoded = self._convert(audio, "wav")
        try:
            with wave.open(io.BytesIO(decoded), "rb") as stream:
                frame_size = stream.getnchannels() * stream.getsampwidth()
                frame_count = stream.getnframes()
                expected_size = frame_count * frame_size
                if frame_size <= 0 or frame_count <= 0 or expected_size > self.max_bytes:
                    raise ValueError("invalid PCM data size")
                frames = stream.readframes(frame_count)
                # WAV's declared frame count is untrusted. A truncated download
                # must not become a nominally long original or short music result.
                if len(frames) != expected_size:
                    raise ValueError("truncated PCM data")
                duration = len(frames) / frame_size / stream.getframerate()
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError("invalid PCM duration")
                return duration
        except (wave.Error, EOFError, ValueError, OverflowError, ZeroDivisionError) as exc:
            raise _music_error(
                "背景音乐文件不完整或已损坏，请重新生成。",
                "music_audio_invalid",
            ) from exc

    def generate(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        if not self.access_key or not self.secret_key:
            raise _music_error(
                "背景音乐服务尚未配置专用访问密钥。当前图片和视频使用的方舟密钥不能用于此音乐接口；"
                "请联系管理员配置火山音乐服务的访问密钥并检查开通状态，完成后可重新生成。",
                "music_credentials_missing",
            )
        prompt = request.prompt.strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("请填写 1 到 2000 字的背景音乐描述")
        duration = request.duration_seconds
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or int(duration) != duration
        ):
            raise ValueError("背景音乐时长请填写整数秒")
        if not 1 <= duration <= self.maximum_duration_seconds:
            raise ValueError(f"背景音乐时长请设置为 1 到 {self.maximum_duration_seconds:g} 秒")
        if request.audio_format not in {"mp3", "wav"}:
            raise ValueError("背景音乐格式仅支持 MP3 或 WAV")
        provider_duration = max(duration, math.ceil(self.minimum_duration_seconds))
        trim_required = duration < provider_duration
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                submitted = self._request(
                    client,
                    "GenBGMForTime" if self.billing == "postpaid" else "GenBGM",
                    {
                        "Text": prompt,
                        "Version": self.model,
                        "Duration": int(provider_duration),
                        "EnableInputRewrite": False,
                    },
                    deadline,
                )
                task_id = submitted.get("TaskID")
                if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
                    raise _music_error("背景音乐服务没有返回生成任务，请稍后重试。")
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _music_error(
                            "背景音乐生成等待超时，请稍后重试；再次生成会提交新的音乐任务。",
                            "music_timeout",
                        )
                    time.sleep(min(self.poll_interval_seconds, remaining))
                    result = self._request(client, "QuerySong", {"TaskID": task_id}, deadline)
                    state = result.get("Status")
                    if state == 3:
                        failure = result.get("FailureReason") or {}
                        raise _supplier_error(
                            failure.get("Code") if isinstance(failure, dict) else None
                        )
                    if state == 2:
                        break
                    if state not in (0, 1):
                        raise _music_error("背景音乐服务返回了无法识别的进度，请稍后重试。")
                detail = result.get("SongDetail") or {}
                url = detail.get("AudioUrl") if isinstance(detail, dict) else None
                if not isinstance(url, str) or not url:
                    raise _music_error("背景音乐生成完成，但没有可下载的音频，请稍后重试。")
                audio = self._download(client, url, deadline)
        except httpx.TimeoutException as exc:
            raise _music_error("背景音乐服务响应超时，请稍后重试。", "music_timeout") from exc
        except httpx.HTTPError as exc:
            raise _music_error("背景音乐服务连接或下载失败，请检查网络后重试。") from exc
        source_duration = self._decoded_duration(audio)
        if trim_required and source_duration + 0.001 < duration:
            raise _music_error(
                f"音乐服务返回的音频只有 {source_duration:g} 秒，不足所选的 {duration:g} 秒，"
                "无法制作本次短配乐，请重新生成。",
                "music_result_too_short",
            )
        audio = self._convert(
            audio,
            request.audio_format,
            trim_duration_seconds=duration if trim_required else None,
        )
        actual_duration = self._decoded_duration(audio)
        if trim_required and abs(actual_duration - duration) > 0.02:
            raise _music_error(
                "短配乐裁切后的时长不正确，请重新生成。", "music_trim_duration_mismatch"
            )
        reported_duration = detail.get("Duration")
        if (
            isinstance(reported_duration, bool)
            or not isinstance(reported_duration, (int, float))
            or not math.isfinite(reported_duration)
            or reported_duration <= 0
        ):
            reported_duration = None
        return MusicGenerationResult(
            audio=audio,
            media_type="audio/wav" if request.audio_format == "wav" else "audio/mpeg",
            extension=request.audio_format,
            duration_seconds=actual_duration,
            requested_duration_seconds=duration,
            provider_id=self.status.provider_id,
            model=self.model,
            metadata={
                "instrumental": True,
                "provider_task_id": task_id,
                "duration_control": "provider-duration-with-trim",
                "supplier_response_format": "url",
                "provider_requested_duration_seconds": provider_duration,
                "provider_reported_duration_seconds": reported_duration,
                "source_duration_seconds": source_duration,
                "trim_applied": trim_required,
                "trim_duration_seconds": duration if trim_required else None,
                "fade_out_seconds": min(0.2, duration / 5) if trim_required else 0,
            },
        )
