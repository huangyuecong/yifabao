"""Wrapper boundaries and cleanup; real Linux Writer roundtrips run in release preflight."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from test_content_chat import OTHER, OWNER, URL
from test_content_chat import chat_client as chat_client

from content_service import chat_documents, chat_office


def source_file(tmp_path, content=chat_office.OLE_HEADER + b"test-document"):
    source = tmp_path / "input.doc"
    source.write_bytes(content)
    return source


def fake_process(monkeypatch, *, result_bytes=None, status=0, timeout=False, group_gone=False):
    observed = []
    killed = []
    monkeypatch.setattr(chat_office, "_executable", lambda: "/usr/bin/libreoffice")

    class Process:
        pid = 991
        waited = False

        def __init__(self, command, **kwargs):
            self.command = command
            directory = Path(command[3])
            observed.append((directory, kwargs))
            assert (directory / "profile/user/registrymodifications.xcu").is_file()
            assert command[-1] == str(directory / "input.doc") or command[-1] == str(
                directory / "input.docx"
            )
            target = command[-2].split(":", 1)[0]
            if result_bytes is not None:
                (directory / "output" / ("input." + target)).write_bytes(result_bytes)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self, timeout=None):
            if timeout is not None and not self.waited and timeout:
                self.waited = True
                if fake_process_timeout:
                    raise subprocess.TimeoutExpired(self.command, timeout)
            return status

    fake_process_timeout = timeout

    def killpg(pid, sig):
        killed.append((pid, sig))
        if group_gone:
            raise ProcessLookupError()

    monkeypatch.setattr(chat_office.subprocess, "Popen", Process)
    monkeypatch.setattr(chat_office.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(chat_office.signal, "SIGKILL", 9, raising=False)
    return observed, killed


@pytest.mark.parametrize(
    "target,content",
    [
        ("doc", chat_office.OLE_HEADER + b"Word"),
        ("pdf", b"%PDF-1.7\nfixture"),
        ("txt", "中文正文".encode()),
    ],
)
def test_conversion_checks_result_and_removes_private_working_directory(
    tmp_path, monkeypatch, target, content
):
    observed, _ = fake_process(monkeypatch, result_bytes=content)
    destination = tmp_path / ("result." + target)
    chat_office.convert_office(source_file(tmp_path), destination, target=target)
    assert destination.read_bytes() == content
    directory, options = observed[0]
    assert not directory.exists()
    assert options["start_new_session"] is True
    assert options["stdin"] == subprocess.DEVNULL
    assert set(options["env"]) == {"PATH", "LANG", "LC_ALL", "SAL_USE_VCLPLUGIN"}


@pytest.mark.parametrize(
    "target,content", [("doc", b"{\\rtf1 fake DOC}"), ("pdf", b"not a PDF"), ("txt", b"")]
)
def test_invalid_output_is_not_published_and_temporary_files_are_removed(
    tmp_path, monkeypatch, target, content
):
    observed, _ = fake_process(monkeypatch, result_bytes=content)
    destination = tmp_path / ("result." + target)
    with pytest.raises(chat_office.OfficeConversionError):
        chat_office.convert_office(source_file(tmp_path), destination, target=target)
    assert not destination.exists()
    assert not observed[0][0].exists()


@pytest.mark.parametrize("group_gone", [False, True])
def test_timeout_cleans_process_group_and_releases_capacity(tmp_path, monkeypatch, group_gone):
    observed, killed = fake_process(monkeypatch, timeout=True, group_gone=group_gone)
    destination = tmp_path / "result.doc"
    with pytest.raises(chat_office.OfficeConversionError, match="超时"):
        chat_office.convert_office(source_file(tmp_path), destination, target="doc")
    assert killed == [(991, 9)]
    assert not destination.exists()
    assert not observed[0][0].exists()
    monkeypatch.setattr(chat_office, "_convert_office", lambda *args, **kwargs: None)
    chat_office.convert_office(source_file(tmp_path), destination, target="doc")
    chat_office.convert_office(source_file(tmp_path), destination, target="doc")


def test_heavy_conversion_concurrency_is_limited_and_capacity_recovers(tmp_path, monkeypatch):
    first, second, release = Event(), Event(), Event()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(True)
        (first if len(calls) == 1 else second).set()
        assert release.wait(5)

    monkeypatch.setattr(chat_office, "_convert_office", blocked)
    source = source_file(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(
            chat_office.convert_office, source, tmp_path / "one.doc", target="doc"
        )
        two = executor.submit(
            chat_office.convert_office, source, tmp_path / "two.doc", target="doc"
        )
        try:
            assert first.wait(2) and second.wait(2)
            with pytest.raises(chat_office.OfficeConversionError, match="稍后重试"):
                chat_office.convert_office(source, tmp_path / "three.doc", target="doc")
        finally:
            release.set()
        one.result()
        two.result()
    chat_office.convert_office(source, tmp_path / "four.doc", target="doc")
    assert len(calls) == 3


def test_unsupported_or_oversized_input_fails_before_process_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_office, "_executable", lambda: pytest.fail("invalid input executed converter")
    )
    source = source_file(tmp_path)
    with pytest.raises(chat_office.OfficeConversionError):
        chat_office.convert_office(source, tmp_path / "bad.html", target="html")
    with source.open("wb") as stream:
        stream.truncate(chat_office.MAX_OFFICE_BYTES + 1)
    with pytest.raises(chat_office.OfficeConversionError):
        chat_office.convert_office(source, tmp_path / "large.doc", target="doc")


def test_legacy_upload_validates_header_and_encoding(tmp_path, monkeypatch):
    source = source_file(tmp_path, b"PK-not-a-doc")
    with pytest.raises(chat_office.OfficeConversionError, match="格式无效"):
        chat_office.extract_legacy_doc(source)
    source.write_bytes(chat_office.OLE_HEADER + b"fixture")
    monkeypatch.setattr(
        chat_office,
        "convert_office",
        lambda source, output, **kwargs: output.write_bytes(b"\xff\xfe"),
    )
    with pytest.raises(chat_office.OfficeConversionError, match="文字编码无效"):
        chat_office.extract_legacy_doc(source)


def test_doc_upload_remains_owner_scoped_and_exports_keep_actionable_failures(
    chat_client, monkeypatch
):
    client, settings, _ = chat_client
    monkeypatch.setattr(chat_office, "extract_legacy_doc", lambda source: "已批准的 DOC 原文事实")
    uploaded = client.post(
        URL + "/attachments",
        headers=OWNER,
        files={"file": ("材料.doc", chat_office.OLE_HEADER + b"fixture", "application/msword")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()
    assert document["kind"] == "document"
    assert "DOC 原文事实" in document["text_preview"]
    assert client.get(document["url"], headers=OTHER).status_code == 404
    assert client.get(document["url"], headers=OWNER).content.startswith(chat_office.OLE_HEADER)

    def unavailable(*args, **kwargs):
        raise chat_office.OfficeConversionError("文档转换任务较多，请稍后重试")

    monkeypatch.setattr(chat_office, "convert_office", unavailable)
    output = settings.data_dir / "failed-output"
    with pytest.raises(chat_documents.ChatDocumentError, match="稍后重试"):
        chat_documents.create_chat_document(output, content="正文", format="doc")
    assert not list(output.iterdir())
