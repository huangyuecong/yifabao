import io
import json
from dataclasses import replace

import pytest
from docx import Document

from content_service.db import SessionLocal
from content_service.generation import compliance_result, generation_source_content
from content_service.models import Artifact
from content_service.publishable_text import clean_text_content
from test_content_compliance_hits import _session_with_phrase
from test_content_llm_helpers import _provider, _request


@pytest.mark.parametrize("include_obsolete_summary", [False, True])
@pytest.mark.parametrize("operation", ["topics", "copies", "transform_copy"])
def test_model_contract_accepts_title_body_without_attached_summary(monkeypatch, operation, include_obsolete_summary):
    candidate = {"title": "新标题", "body": "新的发布正文", "source_ids": []}
    if include_obsolete_summary:
        candidate["summary"] = "避免使用绝对化用语"
    provider = _provider()
    captured = []

    def request_json(op, payload, validate):
        captured.append(payload)
        value = {"candidate": candidate} if op == "transform_copy" else {"candidates": [candidate]}
        return validate(value), "stop", {}

    monkeypatch.setattr(provider, "_request_json", request_json)
    request = replace(_request(), source_content={"title": "旧标题", "body": "旧正文", "summary": "隐藏摘要"})
    result = getattr(provider, operation)(request, "rewrite" if operation == "transform_copy" else 1)
    assert "summary" not in json.dumps(result.content, ensure_ascii=False)
    assert "summary" not in json.dumps(captured, ensure_ascii=False)


def test_old_summary_is_ignored_but_real_title_and_body_violations_still_detected():
    with _session_with_phrase("绝对") as session:
        old = {"candidates": [{"title": "日常之选", "body": "轻松生活", "summary": "避免使用绝对化用语"}]}
        assert compliance_result(session, old) == {"passed": True, "hits": []}
        old["candidates"][0]["body"] = "这是绝对可靠的选择"
        result = compliance_result(session, old)
        assert result["passed"] is False
        assert len(result["hits"]) == 1
        assert result["hits"][0]["field"] == "content.candidates[0].body"


def test_legacy_cleaning_preserves_references_and_removes_only_summary_hits():
    old = {
        "title": "正常标题", "summary": "旧摘要", "body": "正常正文",
        "references": [{"id": "reference", "summary": "知识库摘要保留"}],
        "compliance": {"passed": False, "hits": [{"field": "content.summary", "rule_type": "forbidden"}]},
    }
    cleaned = clean_text_content(old)
    assert "summary" not in cleaned
    assert cleaned["references"] == old["references"]
    assert cleaned["compliance"] == {"passed": True, "hits": []}
    assert old["summary"] == "旧摘要"
    assert "summary" not in generation_source_content(old)


def test_new_storage_and_legacy_read_compliance_and_exports_are_summary_free(content_client, user_headers):
    task = content_client.post('/api/v1/tasks', headers=user_headers, json={
        'mode': 'copy', 'name': '摘要清理回归', 'topic': '成年人的日常生活', 'audience': '成年消费者',
    }).json()
    response = content_client.post(f"/api/v1/tasks/{task['id']}/generate-copy", headers=user_headers, json={'count': 1})
    assert response.status_code == 200, response.text
    artifact = response.json()
    with SessionLocal() as session:
        row = session.get(Artifact, artifact['id'])
        content = json.loads(row.content_json)
        assert 'summary' not in json.dumps(content)
        content['candidates'][0]['summary'] = '旧版附带说明：避免使用绝对化用语'
        row.content_json = json.dumps(content, ensure_ascii=False)
        session.commit()
    confirmed = content_client.post(f"/api/v1/artifacts/{artifact['id']}/confirm", headers=user_headers)
    assert confirmed.status_code == 200, confirmed.text
    assert 'summary' not in json.dumps(confirmed.json()['content'])
    check = content_client.post(f"/api/v1/artifacts/{artifact['id']}/compliance-check", headers=user_headers)
    assert check.status_code == 200
    assert all(not hit['field'].endswith('.summary') for hit in check.json()['hits'])
    for file_format in ['txt', 'md', 'docx']:
        download = content_client.get(f"/api/v1/artifacts/{artifact['id']}/download?format={file_format}", headers=user_headers)
        assert download.status_code == 200, download.text
        text = '\n'.join(p.text for p in Document(io.BytesIO(download.content)).paragraphs) if file_format == 'docx' else download.content.decode('utf-8')
        assert '旧版附带说明' not in text
        assert content['candidates'][0]['title'] in text
        assert content['candidates'][0]['body'] in text
