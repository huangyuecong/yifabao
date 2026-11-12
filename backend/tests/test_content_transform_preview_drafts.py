import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from content_service.db import SessionLocal, engine
from content_service.llm_adapters import ProviderCallError
from content_service.main import app, get_text_provider
from content_service.models import Artifact, ArtifactLink, ContentTask, GenerationRun, TaskStage
from content_service.providers import MockTextProvider, selected_text_candidate
from content_service.services import dump, load


class RecordingTextProvider(MockTextProvider):
    def __init__(self):
        self.requests = []

    def transform_copy(self, request, operation):
        self.requests.append(copy.deepcopy(request))
        return super().transform_copy(request, operation)


@pytest.fixture
def provider():
    value = RecordingTextProvider()
    previous = app.dependency_overrides.get(get_text_provider)
    app.dependency_overrides[get_text_provider] = lambda: value
    yield value
    if previous is None:
        app.dependency_overrides.pop(get_text_provider, None)
    else:
        app.dependency_overrides[get_text_provider] = previous


def create_source(client, headers, *, mode="copy", kind="copy", canonical_title=False):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"name": f"预览隔离-{uuid4().hex[:8]}", "mode": mode},
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    candidate = {"title": "原来的标题", "summary": "原来的摘要", "source_ids": ["trusted"]}
    if kind == "copy":
        candidate["body"] = "原来的正文"
    content = {
        "candidates": [{"title": "未选中的标题"}, candidate],
        "selected_candidate_index": 1,
        "selected_candidate": dict(candidate),
        "reference_item_ids": ["trusted"],
        "references": [{"id": "trusted", "title": "可信资料", "data_level": "internal"}],
        "reference_mode": "provider_declared",
        "content_data_level": "internal",
        "provider": "original-provider",
    }
    with SessionLocal() as session:
        if canonical_title:
            title = Artifact(
                task_id=task_id,
                kind="topic",
                status="confirmed",
                version=1,
                content_json=dump({"candidates": [{"title": candidate["title"]}]}),
                created_by=headers["X-User-Id"],
            )
            session.add(title)
            session.flush()
            title_stage = session.scalar(
                select(TaskStage).where(
                    TaskStage.task_id == task_id,
                    TaskStage.stage_key == "topic",
                )
            )
            title_stage.state = "confirmed"
            title_stage.confirmed_artifact_ids_json = dump({"topic": [title.id]})
            content.update(source_title_artifact_id=title.id, source_title_version=title.version)
        artifact = Artifact(
            task_id=task_id,
            kind=kind,
            version=1,
            content_json=dump(content),
            created_by=headers["X-User-Id"],
        )
        session.add(artifact)
        session.commit()
        return task_id, artifact.id, content


def snapshot(*task_ids):
    result = {}
    with SessionLocal() as session:
        for model in (ContentTask, TaskStage, Artifact, GenerationRun):
            column = model.id if model is ContentTask else model.task_id
            rows = session.scalars(
                select(model).where(column.in_(task_ids)).order_by(model.id)
            ).all()
            result[model.__tablename__] = [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in rows
            ]
    return result


@contextmanager
def preview_writes():
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def preview(client, headers, artifact_id, draft, **overrides):
    return client.post(
        f"/api/v1/artifacts/{artifact_id}/transform-copy",
        headers=headers,
        json={
            "preview": True,
            "operation": "rewrite",
            "instruction": "表达更温暖",
            "draft_content": draft,
            "expected_version": 1,
            **overrides,
        },
    )


@pytest.mark.parametrize("mode", ["full", "copy"])
@pytest.mark.parametrize("kind", ["topic", "copy"])
def test_two_previews_use_unsaved_text_without_versions_until_explicit_save(
    content_client,
    user_headers,
    provider,
    mode,
    kind,
):
    task_id, artifact_id, content = create_source(
        content_client, user_headers, mode=mode, kind=kind
    )
    before = snapshot(task_id)
    draft = copy.deepcopy(content)
    field = "body" if kind == "copy" else "title"
    draft["candidates"][1][field] = "我在弹窗里手动改的内容"
    with preview_writes() as writes:
        first = preview(content_client, user_headers, artifact_id, draft)
        assert first.status_code == 200, first.text
        next_draft = first.json()["content"]
        next_draft["candidates"][0][field] += "，然后继续手工修改"
        second = preview(content_client, user_headers, artifact_id, next_draft)
        assert second.status_code == 200, second.text
    assert len(writes) == 2
    assert all(statement.startswith("INSERT INTO audit_logs") for statement in writes)
    assert snapshot(task_id) == before  # Closing without saving requires no mutation endpoint.
    assert (
        selected_text_candidate(provider.requests[0].source_content)[field]
        == "我在弹窗里手动改的内容"
    )
    assert selected_text_candidate(provider.requests[1].source_content)[field].endswith(
        "然后继续手工修改"
    )
    assert "id" not in second.json()
    saved = content_client.patch(
        f"/api/v1/artifacts/{artifact_id}",
        headers=user_headers,
        json={"expected_version": 1, "content": second.json()["content"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["id"] != artifact_id
    assert saved.json()["version"] == 2
    assert saved.json()["parent_id"] == artifact_id
    after = snapshot(task_id)
    assert len(after["artifacts"]) == len(before["artifacts"]) + 1
    assert (
        next(row for row in after["artifacts"] if row["id"] == artifact_id)
        == before["artifacts"][0]
    )


def test_preview_preserves_authoritative_copy_title_and_trusted_provenance(
    content_client,
    user_headers,
    provider,
):
    task_id, artifact_id, content = create_source(
        content_client,
        user_headers,
        mode="full",
        canonical_title=True,
    )
    before = snapshot(task_id)
    draft = copy.deepcopy(content)
    draft["candidates"][1].update(
        title="伪造标题", body="新的手写正文", source_ids=["trusted", "forged"]
    )
    draft.update(
        content_data_level="public",
        source_title_artifact_id="forged-title",
        references=[{"id": "forged", "title": "伪造资料", "data_level": "public"}],
        reference_item_ids=["forged"],
        provider="forged-provider",
    )
    response = preview(
        content_client, user_headers, artifact_id, draft, instruction="修改标题和正文"
    )
    assert response.status_code == 200, response.text
    requested = selected_text_candidate(provider.requests[-1].source_content)
    assert requested["title"] == "原来的标题"
    assert requested["body"] == "新的手写正文"
    assert requested["source_ids"] == ["trusted"]
    generated = response.json()["content"]
    assert generated["candidates"][0]["title"] == "原来的标题"
    assert generated["source_title_artifact_id"] == content["source_title_artifact_id"]
    assert generated["content_data_level"] == "internal"
    assert "forged" not in generated["reference_item_ids"]
    assert snapshot(task_id) == before


@pytest.mark.parametrize(
    "overrides,status",
    [
        ({"expected_version": 2}, 409),
        ({"expected_version": None}, 422),
        ({"preview": False}, 422),
        ({"draft_content": {}}, 422),
        ({"draft_content": {"title": ["不是文字"]}}, 422),
    ],
)
def test_invalid_preview_draft_never_calls_provider_or_changes_content(
    content_client,
    user_headers,
    provider,
    overrides,
    status,
):
    task_id, artifact_id, content = create_source(content_client, user_headers)
    before = snapshot(task_id)
    response = preview(content_client, user_headers, artifact_id, content, **overrides)
    assert response.status_code == status, response.text
    assert not provider.requests
    assert snapshot(task_id) == before


def test_linked_preview_checks_both_owners_and_keeps_source_and_target_unchanged(
    content_client,
    user_headers,
    other_headers,
    provider,
):
    source_task, artifact_id, content = create_source(content_client, user_headers)
    target_task, _, _ = create_source(content_client, user_headers)
    foreign_task, foreign_artifact, _ = create_source(content_client, other_headers)
    before = snapshot(source_task, target_task, foreign_task)
    assert (
        preview(
            content_client, user_headers, artifact_id, content, target_task_id=target_task
        ).status_code
        == 422
    )
    assert (
        preview(
            content_client, user_headers, artifact_id, content, target_task_id=foreign_task
        ).status_code
        == 403
    )
    assert preview(content_client, other_headers, artifact_id, content).status_code == 403
    assert preview(content_client, user_headers, foreign_artifact, content).status_code == 403
    assert not provider.requests
    with SessionLocal() as session:
        session.add(
            ArtifactLink(
                target_task_id=target_task,
                source_task_id=source_task,
                artifact_id=artifact_id,
                stage_key="copy",
                created_by=user_headers["X-User-Id"],
            )
        )
        session.commit()
    response = preview(
        content_client, user_headers, artifact_id, content, target_task_id=target_task
    )
    assert response.status_code == 200, response.text
    assert snapshot(source_task, target_task, foreign_task) == before
    saved = content_client.patch(
        f"/api/v1/artifacts/{artifact_id}",
        headers=user_headers,
        json={
            "expected_version": 1,
            "target_task_id": target_task,
            "content": response.json()["content"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["task_id"] == target_task
    assert saved.json()["parent_id"] == artifact_id
    assert snapshot(source_task)["artifacts"] == [
        row for row in before["artifacts"] if row["task_id"] == source_task
    ]
    with SessionLocal() as session:
        assert load(session.get(Artifact, artifact_id).content_json) == content


def test_legacy_full_copy_preview_does_not_initialize_or_normalize_workflow(
    content_client,
    user_headers,
    provider,
):
    with SessionLocal() as session:
        task = ContentTask(
            owner_id=user_headers["X-User-Id"],
            mode="full",
            name="旧文案预览",
            current_stage="storyboard",
        )
        session.add(task)
        session.flush()
        content = {"candidates": [{"title": "旧标题", "body": "旧正文"}]}
        artifact = Artifact(
            task_id=task.id, kind="copy", content_json=dump(content), created_by=task.owner_id
        )
        session.add(artifact)
        session.commit()
        task_id, artifact_id = task.id, artifact.id
    before = snapshot(task_id)
    with preview_writes() as writes:
        response = preview(content_client, user_headers, artifact_id, content)
    assert response.status_code == 200, response.text
    assert len(writes) == 1 and writes[0].startswith("INSERT INTO audit_logs")
    assert snapshot(task_id) == before


def test_non_preview_transform_still_creates_generation_and_version(
    content_client,
    user_headers,
    provider,
):
    task_id, artifact_id, _content = create_source(content_client, user_headers)
    before = snapshot(task_id)
    response = content_client.post(
        f"/api/v1/artifacts/{artifact_id}/transform-copy",
        headers=user_headers,
        json={"operation": "rewrite", "instruction": "更亲切", "preview": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] != artifact_id
    after = snapshot(task_id)
    assert len(after["artifacts"]) == len(before["artifacts"]) + 1
    assert len(after["generation_runs"]) == len(before["generation_runs"]) + 1


@pytest.mark.parametrize("failure", ["provider_error", "unchanged"])
def test_failed_preview_cannot_create_failure_versions_or_mutate_stages(
    content_client,
    user_headers,
    provider,
    monkeypatch,
    failure,
):
    task_id, artifact_id, content = create_source(content_client, user_headers)
    before = snapshot(task_id)

    def transform(request, operation):
        if failure == "provider_error":
            raise ProviderCallError("测试模型调用失败", code="test_failure", retryable=False)
        generated = MockTextProvider().transform_copy(request, operation)
        return replace(generated, content=dict(selected_text_candidate(request.source_content)))

    monkeypatch.setattr(provider, "transform_copy", transform)
    with preview_writes() as writes:
        response = preview(content_client, user_headers, artifact_id, content)
    assert response.status_code == 502, response.text
    assert not writes
    assert snapshot(task_id) == before


def test_preview_draft_cannot_lower_original_data_level_for_external_provider(
    content_client,
    user_headers,
    provider,
):
    task_id, artifact_id, content = create_source(content_client, user_headers)
    with SessionLocal() as session:
        content["content_data_level"] = "restricted"
        session.get(Artifact, artifact_id).content_json = dump(content)
        session.commit()
    provider.status = replace(provider.status, external=True)
    before = snapshot(task_id)
    draft = copy.deepcopy(content)
    draft["content_data_level"] = "public"
    draft["references"] = []
    draft["reference_item_ids"] = []
    response = preview(content_client, user_headers, artifact_id, draft)
    assert response.status_code == 403, response.text
    assert "未获准外发" in response.json()["detail"]
    assert not provider.requests
    assert snapshot(task_id) == before


def test_preview_draft_contract_matches_optional_runtime_fields():
    contract_path = Path(__file__).resolve().parents[1] / "contracts" / "content.openapi.json"
    checked_in = json.loads(contract_path.read_text(encoding="utf-8"))["components"]["schemas"]
    runtime = app.openapi()["components"]["schemas"]
    for name in ("draft_content", "expected_version"):
        assert (
            checked_in["TransformCopyRequest"]["properties"][name]
            == runtime["TransformCopyRequest"]["properties"][name]
        )
        assert name not in runtime["TransformCopyRequest"].get("required", [])
