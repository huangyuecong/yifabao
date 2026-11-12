"""Original-file boundary: scoped reads, byte identity and failed restore safety."""

import hashlib
import json
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "services" / "content_service" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from content_service import weknora
from content_service.weknora import WeKnoraClient, WeKnoraError

ORIGINAL = b"%PDF-1.4\n\x00\xff binary original, not extracted text\n%%EOF\n"
MARKER = "original:asset-1:version-1"


def document(**changes):
    return {
        "id": "doc-1",
        "knowledge_base_id": "kb-1",
        "parse_status": "failed",  # A failed parse must not make the source unrecoverable.
        "enable_status": "disabled",
        "metadata": {"local_source_marker": MARKER},
        **changes,
    }


def response(value):
    return httpx.Response(200, json={"success": True, "data": value})


def client(handler, **kwargs):
    return WeKnoraClient(
        "https://knowledge.example.test", "private-api-key", "kb-1",
        http_transport=httpx.MockTransport(handler), **kwargs,
    )


def restore(api, destination):
    api.download_document(
        "doc-1", destination, expected_sha256=hashlib.sha256(ORIGINAL).hexdigest(),
        expected_size=len(ORIGINAL), source_marker=MARKER,
    )


class FileStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_download_failed_parse_restores_exact_original_and_scopes_both_requests(tmp_path):
    target = tmp_path / "original.pdf"
    target.write_bytes(b"previous cached file")
    seen = []
    stream = FileStream([ORIGINAL[:9], ORIGINAL[9:]])

    def handler(request):
        seen.append(request.url.path)
        assert request.method == "GET"
        assert request.url.host == "knowledge.example.test"
        assert request.headers["X-API-Key"] == "private-api-key"
        assert request.headers["X-Tenant-ID"] == "10008"
        if len(seen) == 1:
            return response(document())
        assert request.headers["Accept-Encoding"] == "identity"
        # Remote filename must never control the local destination.
        return httpx.Response(200, stream=stream, headers={
            "Content-Length": str(len(ORIGINAL)),
            "Content-Disposition": 'attachment; filename="../../another.pdf"',
        })

    restore(client(handler, tenant_id="10008"), target)
    assert seen == ["/api/v1/knowledge/doc-1", "/api/v1/knowledge/doc-1/download"]
    assert target.read_bytes() == ORIGINAL
    assert list(tmp_path.iterdir()) == [target]
    assert stream.closed


@pytest.mark.parametrize("change, code", [
    ({"knowledge_base_id": "someone-elses-kb"}, "knowledge_base_mismatch"),
    ({"id": "someone-elses-document"}, "document_id_mismatch"),
    ({"metadata": {"local_source_marker": "different-version"}}, "file_source_mismatch"),
    ({"metadata": {}}, "file_source_mismatch"),
])
def test_download_rejects_unowned_source_before_requesting_bytes(tmp_path, change, code):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return response(document(**change))

    with pytest.raises(WeKnoraError, match=".") as error:
        restore(client(handler), tmp_path / "source.pdf")
    assert error.value.code == code
    assert calls == ["/api/v1/knowledge/doc-1"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("body, headers", [
    (ORIGINAL[:-1], {}),
    (ORIGINAL + b"unexpected", {}),
    (b"x" * len(ORIGINAL), {}),
    (ORIGINAL, {"Content-Length": str(len(ORIGINAL) + 1)}),
    (ORIGINAL, {"Content-Length": "invalid"}),
    (ORIGINAL, {"Content-Encoding": "gzip"}),
])
def test_bad_original_never_replaces_existing_file_or_leaves_partial(tmp_path, body, headers):
    target = tmp_path / "original.pdf"
    target.write_bytes(b"keep existing")
    stream = FileStream([body[:10], body[10:]])

    def handler(request):
        if request.url.path.endswith("/download"):
            return httpx.Response(200, headers=headers, stream=stream)
        return response(document())

    with pytest.raises(WeKnoraError) as error:
        restore(client(handler), target)
    assert error.value.code == "file_integrity_failed"
    assert target.read_bytes() == b"keep existing"
    assert list(tmp_path.iterdir()) == [target]
    assert stream.closed


def test_redirect_is_not_followed_and_error_does_not_include_response_or_key(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/download"):
            return httpx.Response(302, headers={
                "Location": "https://different-host.invalid/?key=private-api-key"
            }, content=b"private response body")
        return response(document())

    with pytest.raises(WeKnoraError) as error:
        restore(client(handler), tmp_path / "original.pdf")
    assert error.value.status_code == 302
    assert len(calls) == 2
    assert "private" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_stream_failure_cleans_partial_and_hides_transport_details(tmp_path):
    class FailedStream(FileStream):
        def __iter__(self):
            yield ORIGINAL[:10]
            raise httpx.ReadError("key=private-api-key; provider secret response")

    def handler(request):
        if request.url.path.endswith("/download"):
            return httpx.Response(200, stream=FailedStream([]))
        return response(document())

    with pytest.raises(WeKnoraError) as error:
        restore(client(handler), tmp_path / "original.pdf")
    assert error.value.code == "connection_failed"
    assert "private" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_total_deadline_expires_during_slow_stream_and_cleans_partial(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(weknora.time, "monotonic", lambda: clock[0])

    class SlowStream(FileStream):
        def __iter__(self):
            for chunk in [ORIGINAL[:10], ORIGINAL[10:]]:
                clock[0] += 0.6
                yield chunk

    def handler(request):
        if request.url.path.endswith("/download"):
            return httpx.Response(200, stream=SlowStream([]))
        return response(document())

    with pytest.raises(WeKnoraError) as error:
        restore(client(handler, timeout_seconds=1), tmp_path / "original.pdf")
    assert error.value.code == "timeout"
    assert list(tmp_path.iterdir()) == []


def multipart(request):
    raw = ("Content-Type: " + request.headers["Content-Type"] + "\r\n\r\n").encode()
    parsed = BytesParser(policy=policy.default).parsebytes(raw + request.content)
    return {
        part.get_param("name", header="content-disposition"): part
        for part in parsed.iter_parts()
    }


def test_original_upload_keeps_exact_bytes_suffix_marker_and_optional_process_config(tmp_path):
    original = tmp_path / "企业说明.PDF"
    original.write_bytes(ORIGINAL)
    settings = {"summary_enabled": False, "graph_enabled": False}

    def handler(request):
        assert request.url.path == "/api/v1/knowledge-bases/kb-1/knowledge/file"
        parts = multipart(request)
        assert parts["file"].get_payload(decode=True) == ORIGINAL
        assert parts["file"].get_filename() == original.name
        assert parts["fileName"].get_payload(decode=True).decode().endswith(".pdf")
        assert json.loads(parts["process_config"].get_payload(decode=True)) == settings
        assert json.loads(parts["metadata"].get_payload(decode=True)) == {
            "local_source_marker": MARKER, "local_title": "完整资料原件",
        }
        return response(document())

    result = client(handler).ingest_file(
        original, title="完整资料原件", metadata={"local_source_marker": MARKER},
        process_config=settings,
    )
    assert result.id == "doc-1"
    assert original.read_bytes() == ORIGINAL


@pytest.mark.parametrize("marker", ["different-original", None])
def test_original_upload_never_adopts_deduplicated_unowned_file(tmp_path, marker):
    original = tmp_path / "original.pdf"
    original.write_bytes(ORIGINAL)
    calls = []

    def handler(request):
        calls.append(request.method)
        data = document(metadata={"local_source_marker": marker} if marker else {})
        if request.method == "POST":
            return httpx.Response(409, json={"success": False, "data": data})
        return response(data)

    with pytest.raises(WeKnoraError) as error:
        client(handler).ingest_file(original, metadata={"local_source_marker": MARKER})
    assert error.value.code == "duplicate_unowned"
    assert calls == (["POST"] if marker else ["POST", "GET"])


@pytest.mark.parametrize("message,code", [
    ("上传图片文件需要设置VLM模型", "multimodal_not_configured"),
    ("上传音频文件需要设置ASR语音识别模型", "asr_not_configured"),
    ("上传图片文件需要设置VLM模型: private detail", "request_failed"),
    ("unknown failure", "request_failed"),
])
def test_only_exact_file_preflight_error_allows_configuration_retry(tmp_path, message, code):
    source = tmp_path / "source.pdf"
    source.write_bytes(ORIGINAL)
    payload = {"success": False, "error": {"message": message, "details": "private-api-key"}}
    api = client(lambda request: httpx.Response(400, json=payload))
    with pytest.raises(WeKnoraError) as error:
        api.ingest_file(source, metadata={"local_source_marker": MARKER})
    assert error.value.code == code
    assert "private" not in str(error.value)
    # The same remote message at a read endpoint cannot imply a safe upload rejection.
    with pytest.raises(WeKnoraError) as error:
        api.get_document("doc-1")
    assert error.value.code == "request_failed"
