"""Real HTTP chat flow for repeated media requests; providers only are faked."""

from types import SimpleNamespace

import pytest
from test_chat_followup_regressions import png_bytes
from test_content_chat import OTHER, OWNER, URL
from test_content_chat import chat_client as chat_client
from test_content_chat_context_routing import automatic_conversation, automatic_send
from test_content_chat_creation import configure_media, media_settings

from content_service import chat_generation, chat_media, chat_routes


@pytest.fixture
def media_chat(chat_client, monkeypatch):
    client, settings, app = chat_client
    monkeypatch.setattr(chat_routes, "get_runtime_settings", lambda: media_settings(settings))
    configure_media(client, app)
    observed = []

    def generate(settings, model, *, prompt, output_dir, reference_path):
        kind = model["kind"]
        observed.append(
            {
                "kind": kind,
                "prompt": prompt,
                "reference": reference_path.read_bytes() if reference_path else None,
            }
        )
        path = output_dir / ("result.png" if kind == "image" else "result.mp4")
        path.write_bytes(png_bytes() if kind == "image" else b"video-fixture")
        return {
            "path": path,
            "name": path.name,
            "kind": kind,
            "mime_type": "image/png" if kind == "image" else "video/mp4",
            "metadata": {"audio_requested": True, "has_audio": False} if kind == "video" else {},
        }

    monkeypatch.setattr(chat_generation, "generate_chat_media", generate)
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: "这是一条普通文字回答。")
    return client, automatic_conversation(client), observed


@pytest.mark.parametrize(
    "kind,followup",
    [
        ("video", "怎么没声音的，而且我要的是中秋佳节团圆的时候有这个酒在饭桌上呀"),
        ("video", "镜头缓缓推进一张摆满中秋佳肴的圆桌，桌上有一轮明月般的月饼。"),
        ("video", "重新生成"),
        ("video", "再来一个"),
        ("video", "加上欢快的音乐和团圆碰杯声"),
        ("image", "再来一张"),
        ("image", "背景是中秋家宴，暖黄色灯光照在酒瓶上"),
        ("image", "换成红色背景"),
    ],
)
def test_continuations_create_new_downloadable_artifacts(media_chat, kind, followup):
    client, cid, calls = media_chat
    automatic_send(client, cid, "生成一张臻品雅酿图片")
    if kind == "video":
        automatic_send(client, cid, "用这张图片生成视频")
    before = len(calls)
    response = automatic_send(client, cid, followup)
    assert response.status_code == 200, response.text
    reply = response.json()["messages"][-1]
    assert reply["status"] == "completed", reply
    assert reply["artifacts"][-1]["kind"] == kind
    assert len(calls) == before + 1 and calls[-1]["kind"] == kind
    assert calls[-1]["reference"] == png_bytes()
    assert followup in calls[-1]["prompt"] and "臻品雅酿" in calls[-1]["prompt"]
    artifact = reply["artifacts"][-1]
    assert client.get(artifact["url"], headers=OWNER).status_code == 200
    assert client.get(artifact["url"], headers=OTHER).status_code == 404
    retry = client.post(URL + f"/conversations/{cid}/retry", headers=OWNER, json={})
    assert retry.status_code == 200, retry.text
    repeated = retry.json()["messages"][-1]
    assert repeated["status"] == "completed"
    assert len(calls) == before + 2
    assert repeated["artifacts"][-1]["id"] != artifact["id"]
    assert calls[-1]["reference"] == png_bytes()


def test_video_card_revision_uses_original_image_instead_of_video_transport(media_chat):
    client, cid, calls = media_chat
    automatic_send(client, cid, "生成一张图片")
    video = automatic_send(client, cid, "用它生成视频").json()["messages"][-1]["artifacts"][0]
    reply = automatic_send(client, cid, "改为全家举杯的场景", reference_artifact_id=video["id"])
    assert reply.status_code == 200, reply.text
    assert reply.json()["messages"][-1]["artifacts"][0]["kind"] == "video"
    assert calls[-1]["reference"] == png_bytes()


@pytest.mark.parametrize(
    "prompt",
    [
        "谢谢",
        "好的",
        "你好",
        "为什么没有声音",
        "解释一下视频的生成过程",
        "只分析这个视频",
        "不要再生成视频，只写一份文案",
        "这个视频的提示词是什么",
    ],
)
def test_questions_and_cancellations_do_not_start_another_paid_media_job(media_chat, prompt):
    client, cid, calls = media_chat
    automatic_send(client, cid, "生成视频")
    before = len(calls)
    response = automatic_send(client, cid, prompt)
    assert response.status_code == 200, response.text
    assert response.json()["messages"][-1]["artifacts"] == []
    assert len(calls) == before


def test_affirmation_of_proposed_revision_and_unrelated_greeting(media_chat, monkeypatch):
    client, cid, calls = media_chat
    automatic_send(client, cid, "生成视频")
    monkeypatch.setattr(
        chat_generation, "call_chat", lambda *args: "可以生成团圆场景的新视频，需要我制作吗？"
    )
    automatic_send(client, cid, "为什么画面不符合要求")
    reply = automatic_send(client, cid, "好的").json()["messages"][-1]
    assert reply["artifacts"][0]["kind"] == "video"
    monkeypatch.setattr(chat_generation, "call_chat", lambda *args: "你好！")
    automatic_send(client, cid, "你好")
    before = len(calls)
    automatic_send(client, cid, "好的")
    assert len(calls) == before


def test_silent_provider_file_is_delivered_with_honest_audio_notice(media_chat):
    client, cid, _ = media_chat
    reply = automatic_send(client, cid, "生成有声视频").json()["messages"][-1]
    assert reply["artifacts"][0]["kind"] == "video"
    assert "本次已请求生成声音" in reply["content"]
    assert "不含音轨" in reply["content"]


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("生成一个产品视频", True),
        ("怎么没声音，重新生成", True),
        ("不要静音，要有声视频", True),
        ("生成静音视频", False),
        ("生成无声的视频", False),
        ("不要声音", False),
        ("用户：生成静音视频\n本轮用户制作要求（优先遵循）：\n重新做一个有音乐的", True),
    ],
)
def test_audio_preference_only_uses_latest_request(prompt, expected):
    assert chat_media._video_audio_requested(prompt) is expected


@pytest.mark.parametrize("codec", [None, "aac"])
def test_audio_metadata_uses_actual_file_stream_detection(tmp_path, monkeypatch, codec):
    monkeypatch.setattr(
        chat_media,
        "probe_media",
        lambda *args, **kwargs: SimpleNamespace(audio_codec=codec, duration_seconds=5),
    )
    assert chat_media._video_audio_metadata(tmp_path / "video.mp4", True) == {
        "audio_requested": True,
        "has_audio": bool(codec),
        "duration_seconds": 5,
    }


def test_chat_model_cannot_fake_a_new_file_by_copying_a_historical_reply(media_chat, monkeypatch):
    client, cid, calls = media_chat
    automatic_send(client, cid, "生成视频")
    monkeypatch.setattr(
        chat_generation,
        "call_chat",
        lambda *args: (
            "已使用“视频模型”生成视频，可以预览或下载。\n[此轮已生成文件：fake.mp4，类型：video]"
        ),
    )
    reply = automatic_send(client, cid, "你好").json()["messages"][-1]
    assert "没有生成新文件" in reply["content"]
    assert not reply["artifacts"] and len(calls) == 1
