"""The video editors limit generated music to 15s without changing provider limits."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from content_service.media_adapters import MockMusicGenerationPort
from content_service.schemas import MusicGenerationCreateRequest


@pytest.fixture
def recording_music_provider(monkeypatch):
    from content_service.main import app, get_music_generation_port

    provider = MockMusicGenerationPort()
    calls = []
    generate = provider.generate

    def record(request):
        calls.append(request)
        return generate(request)

    monkeypatch.setattr(provider, "generate", record)
    monkeypatch.setitem(app.dependency_overrides, get_music_generation_port, lambda: provider)
    return provider, calls


def _project(client, headers, mode):
    response = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "mode": mode,
            "name": f"配乐时长边界-{uuid4().hex[:8]}",
            "topic": "品牌短片",
            "audience": "成年消费者",
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/tasks/{response.json()['id']}/video-project", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["full", "video"])
@pytest.mark.parametrize("duration", [1, 15, None])
def test_editor_music_accepts_bounds_and_defaults_to_15_without_timeline_changes(
    content_client, user_headers, recording_music_provider, mode, duration
):
    provider, calls = recording_music_provider
    project = _project(content_client, user_headers, mode)
    payload = {
        "expected_revision": project["current_revision"],
        "prompt": "温暖钢琴，无人声",
        "audio_format": "wav",
    }
    if duration is not None:
        payload["duration_seconds"] = duration
    response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-music",
        headers=user_headers,
        json=payload,
    )
    assert response.status_code == 200, response.text
    expected_duration = duration if duration is not None else 15
    assert len(calls) == 1
    assert calls[0].duration_seconds == expected_duration
    assert response.json()["metadata"]["requested_duration_seconds"] == expected_duration
    assert response.json()["metadata"]["duration_seconds"] == expected_duration
    assert response.json()["role"] == "music"
    updated = content_client.get(
        f"/api/v1/video-projects/{project['id']}", headers=user_headers
    ).json()
    assert updated["current_revision_id"] == project["current_revision_id"]
    assert updated["revision"]["timeline"] == project["revision"]["timeline"]
    # The editor's UI/product limit must not reduce the provider's own capability.
    assert provider.maximum_duration_seconds == 300


@pytest.mark.parametrize("mode", ["full", "video"])
@pytest.mark.parametrize("duration", [0, 15.01, 16, 30, 600])
def test_editor_music_rejects_out_of_range_before_generation(
    content_client, user_headers, recording_music_provider, mode, duration
):
    _, calls = recording_music_provider
    project = _project(content_client, user_headers, mode)
    response = content_client.post(
        f"/api/v1/video-projects/{project['id']}/generate-music",
        headers=user_headers,
        json={
            "expected_revision": project["current_revision"],
            "prompt": "温暖钢琴，无人声",
            "duration_seconds": duration,
        },
    )
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "duration_seconds"] for error in response.json()["errors"])
    assert calls == []
    updated = content_client.get(
        f"/api/v1/video-projects/{project['id']}", headers=user_headers
    ).json()
    assert updated["assets"] == project["assets"]
    assert updated["current_revision_id"] == project["current_revision_id"]
    assert updated["revision"]["timeline"] == project["revision"]["timeline"]


def test_editor_music_openapi_duration_matches_runtime_schema():
    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts/content.openapi.json").read_text(
            encoding="utf-8"
        )
    )
    duration = MusicGenerationCreateRequest.model_json_schema()["properties"]["duration_seconds"]
    saved = contract["components"]["schemas"]["MusicGenerationCreateRequest"]["properties"][
        "duration_seconds"
    ]
    for key, value in {"minimum": 1, "maximum": 15, "default": 15}.items():
        assert duration[key] == saved[key] == value
