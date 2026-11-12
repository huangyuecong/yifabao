from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MalwareScanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MalwareScanResult:
    status: str
    scanner: str | None = None
    message: str | None = None


def scan_media_file(
    path: Path,
    *,
    command: str | None,
    timeout_seconds: float = 60,
    required: bool = False,
) -> MalwareScanResult:
    """Scan one server-resolved file without invoking a shell.

    ClamAV-compatible exit codes are used: 0 clean, 1 infected, >1 scanner error.
    Deployments can fail closed with ``required=True``.
    """

    try:
        source = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise MalwareScanError("待扫描媒体文件不存在") from exc
    if not source.is_file():
        raise MalwareScanError("待扫描媒体文件不存在")
    if not command:
        if required:
            raise MalwareScanError("生产环境要求病毒扫描，但未配置扫描器")
        return MalwareScanResult(status="not_configured")
    executable = shutil.which(command)
    if executable is None:
        configured = Path(command).expanduser().resolve()
        executable = str(configured) if configured.is_file() else None
    if executable is None:
        if required:
            raise MalwareScanError("病毒扫描器不可用")
        return MalwareScanResult(status="unavailable", scanner=command)
    if not 0 < timeout_seconds <= 600:
        raise MalwareScanError("病毒扫描超时时间必须在 0 到 600 秒之间")
    try:
        completed = subprocess.run(
            [executable, "--no-summary", str(source)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if required:
            raise MalwareScanError("病毒扫描器执行失败或超时") from exc
        return MalwareScanResult(
            status="unavailable",
            scanner=Path(executable).name,
            message=str(exc)[:500],
        )
    message = " ".join((completed.stdout or completed.stderr or "").split())[-500:]
    if completed.returncode == 0:
        return MalwareScanResult(
            status="clean",
            scanner=Path(executable).name,
            message=message or None,
        )
    if completed.returncode == 1:
        raise MalwareScanError("上传文件未通过病毒扫描")
    if required:
        raise MalwareScanError(f"病毒扫描器异常退出：{message or completed.returncode}")
    return MalwareScanResult(
        status="unavailable",
        scanner=Path(executable).name,
        message=message or f"exit={completed.returncode}",
    )
