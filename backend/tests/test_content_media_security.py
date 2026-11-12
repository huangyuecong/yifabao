from pathlib import Path
from types import SimpleNamespace

import pytest

from content_service.media_security import MalwareScanError, scan_media_file


def test_scan_skips_when_scanner_is_not_configured(tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    result = scan_media_file(source, command=None)
    assert result.status == "not_configured"
    assert result.scanner is None


def test_scan_requires_a_scanner_in_fail_closed_mode(tmp_path: Path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    with pytest.raises(MalwareScanError, match="未配置扫描器"):
        scan_media_file(source, command=None, required=True)


def test_scan_rejects_missing_files(tmp_path: Path):
    with pytest.raises(MalwareScanError, match="不存在"):
        scan_media_file(tmp_path / "missing.mp4", command=None)


def test_scan_reports_unavailable_scanner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr("content_service.media_security.shutil.which", lambda _command: None)
    result = scan_media_file(source, command="clamdscan-missing")
    assert result.status == "unavailable"
    assert result.scanner == "clamdscan-missing"


def test_scan_fail_closed_when_scanner_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr("content_service.media_security.shutil.which", lambda _command: None)
    with pytest.raises(MalwareScanError, match="不可用"):
        scan_media_file(source, command="clamdscan-missing", required=True)


def test_scan_rejects_invalid_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr("content_service.media_security.shutil.which", lambda _command: "clamdscan")
    with pytest.raises(MalwareScanError, match="超时时间"):
        scan_media_file(source, command="clamdscan", timeout_seconds=0)


def test_scan_clean_infected_and_scanner_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr("content_service.media_security.shutil.which", lambda _command: "clamdscan")

    monkeypatch.setattr(
        "content_service.media_security.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="OK", stderr=""),
    )
    clean = scan_media_file(source, command="clamdscan")
    assert clean.status == "clean"
    assert clean.scanner == "clamdscan"

    monkeypatch.setattr(
        "content_service.media_security.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="FOUND"),
    )
    with pytest.raises(MalwareScanError, match="未通过病毒扫描"):
        scan_media_file(source, command="clamdscan")

    monkeypatch.setattr(
        "content_service.media_security.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=2, stdout="", stderr="broken"),
    )
    unavailable = scan_media_file(source, command="clamdscan")
    assert unavailable.status == "unavailable"
    with pytest.raises(MalwareScanError, match="异常退出"):
        scan_media_file(source, command="clamdscan", required=True)


def test_scan_rejects_directories_and_uses_explicit_scanner_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(MalwareScanError, match="不存在"):
        scan_media_file(folder, command=None)

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    scanner = tmp_path / "clamdscan.exe"
    scanner.write_bytes(b"scanner")
    monkeypatch.setattr("content_service.media_security.shutil.which", lambda _command: None)
    monkeypatch.setattr(
        "content_service.media_security.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="OK", stderr=""),
    )
    result = scan_media_file(source, command=str(scanner))
    assert result.status == "clean"
    assert result.scanner == "clamdscan.exe"


def test_scan_timeout_and_oserror_are_unavailable_unless_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import subprocess

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr("content_service.media_security.shutil.which", lambda _command: "clamdscan")

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="clamdscan", timeout=1)

    monkeypatch.setattr("content_service.media_security.subprocess.run", raise_timeout)
    unavailable = scan_media_file(source, command="clamdscan")
    assert unavailable.status == "unavailable"
    with pytest.raises(MalwareScanError, match="超时"):
        scan_media_file(source, command="clamdscan", required=True)

    def raise_oserror(*_args, **_kwargs):
        raise OSError("scanner crashed")

    monkeypatch.setattr("content_service.media_security.subprocess.run", raise_oserror)
    crashed = scan_media_file(source, command="clamdscan")
    assert crashed.status == "unavailable"
    with pytest.raises(MalwareScanError, match="执行失败"):
        scan_media_file(source, command="clamdscan", required=True)
