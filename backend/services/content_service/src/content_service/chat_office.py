"""Bounded, isolated legacy Word import/export using the bundled Writer runtime."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from threading import BoundedSemaphore

OLE_HEADER = bytes.fromhex("d0cf11e0a1b11ae1")
MAX_OFFICE_BYTES = 100 * 1024 * 1024
_conversion_slots = BoundedSemaphore(2)


class OfficeConversionError(ValueError):
    """A safe, actionable conversion failure without user text or host details."""


def _executable() -> str:
    candidate = shutil.which("libreoffice") or shutil.which("soffice")
    if not candidate or sys.platform != "linux":
        raise OfficeConversionError("服务器的 DOC 转换组件尚未安装，请更新服务器文档组件后重试")
    return candidate


def _profile(directory: Path) -> None:
    user = directory / "profile/user"
    user.mkdir(parents=True)
    # A fresh private profile per operation prevents reuse of GUI sessions and
    # macros. The worker additionally denies network sockets before exec.
    (user / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
        '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
        '</item><item oor:path="/org.openoffice.Office.Common/Load">'
        '<prop oor:name="UpdateLink" oor:op="fuse"><value>2</value></prop>'
        "</item></oor:items>",
        encoding="utf-8",
    )


def convert_office(source: Path, destination: Path, *, target: str) -> None:
    """Limit heavyweight conversions across concurrent uploads and chat workers."""
    if not _conversion_slots.acquire(blocking=False):
        raise OfficeConversionError("文档转换任务较多，请稍后重试")
    try:
        _convert_office(source, destination, target=target)
    finally:
        _conversion_slots.release()


def _convert_office(source: Path, destination: Path, *, target: str) -> None:
    """Convert only explicit DOC/DOCX inputs; never accept arbitrary filters."""
    filters = {
        "doc": "doc:MS Word 97",
        "txt": "txt:Text (encoded):UTF8",
        "pdf": "pdf:writer_pdf_Export",
    }
    if target not in filters or source.suffix.lower() not in {".doc", ".docx"}:
        raise OfficeConversionError("文档转换格式无效")
    if (
        not source.is_file()
        or source.is_symlink()
        or not 0 < source.stat().st_size <= MAX_OFFICE_BYTES
    ):
        raise OfficeConversionError("待转换文档无效或超过大小限制")
    executable = _executable()
    with tempfile.TemporaryDirectory(prefix="content-office-") as temporary:
        directory = Path(temporary)
        input_path = directory / ("input" + source.suffix.lower())
        shutil.copyfile(source, input_path)
        output = directory / "output"
        output.mkdir()
        _profile(directory)
        # The helper drops privileges and installs resource/network limits in a
        # fresh process, avoiding unsafe preexec_fn use in background threads.
        command = [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("chat_office_worker.py")),
            str(directory),
            executable,
            filters[target],
            str(input_path),
        ]
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SAL_USE_VCLPLUGIN": "svp",
        }
        try:
            with (
                (directory / "conversion.log").open("wb") as log,
                subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=environment,
                    start_new_session=True,
                ) as process,
            ):
                try:
                    returncode = process.wait(timeout=75)
                except subprocess.TimeoutExpired:
                    # The worker may finish between timeout and group cleanup.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
            result = output / ("input." + target)
            if returncode != 0 or not result.is_file() or result.is_symlink():
                raise OfficeConversionError("文档转换失败，请确认文件未加密、未损坏后重试")
            if not 0 < result.stat().st_size <= MAX_OFFICE_BYTES:
                raise OfficeConversionError("转换后文档为空或超过大小限制")
            if target == "doc":
                with result.open("rb") as stream:
                    if stream.read(8) != OLE_HEADER:
                        raise OfficeConversionError("DOC 转换未返回有效的 Word 文件")
            if target == "pdf":
                with result.open("rb") as stream:
                    if stream.read(5) != b"%PDF-":
                        raise OfficeConversionError("PDF 转换未返回有效的 PDF 文件")
            shutil.copyfile(result, destination)
        except subprocess.TimeoutExpired as exc:
            raise OfficeConversionError("文档转换超时，请拆分文档后重试") from exc
        except OSError as exc:
            raise OfficeConversionError("文档转换组件无法运行，请检查服务器更新结果") from exc


def extract_legacy_doc(source: Path) -> str:
    with source.open("rb") as stream:
        header = stream.read(8)
    if header != OLE_HEADER and not header.startswith(b"{\\rtf"):
        raise OfficeConversionError("DOC 文件格式无效，请上传真实的 Word 97–2003 文档")
    with tempfile.TemporaryDirectory(prefix="content-doc-text-") as temporary:
        output = Path(temporary) / "document.txt"
        convert_office(source, output, target="txt")
        if output.stat().st_size > 4 * 1024 * 1024:
            raise OfficeConversionError("文档文字过多，请拆分后上传")
        try:
            text = output.read_text(encoding="utf-8-sig")
        except UnicodeError:
            raise OfficeConversionError(
                "DOC 转换文字编码无效，请重新另存为 Word 文档后上传"
            ) from None
    if not text.strip():
        raise OfficeConversionError("DOC 未提取到文字，请确认文档包含正文且未加密")
    return text.strip()
