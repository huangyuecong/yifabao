"""Catalog flow against a real database: products -> generation -> review -> publish records."""

import io
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent_core import Identity
from content_service import (
    automation_content,
    automation_executor,
    catalog_generation,
    catalog_publishing,
    catalog_video,
    db,
)
from content_service import catalog_routes as routes
from content_service.catalog_models import (
    ContentMedia,
    GenerationBatch,
    GenerationItem,
    GenerationMedia,
    Product,
    ProductContent,
    ProductImage,
    PublishRecord,
)
from content_service.models import AuditLog, SensitiveRule

BASE = "/api/v1/catalog"
HEADERS = {"X-Content-Config-Request": "1"}
ALICE = Identity("alice", "Alice", "marketing", frozenset({"admin"}))


def png_bytes(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}", connect_args={"check_same_thread": False})
    for model in (AuditLog, SensitiveRule, Product, ProductImage, GenerationBatch, GenerationItem,
                  GenerationMedia, ProductContent, ContentMedia, PublishRecord):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    # Run background work inline so tests observe the final state.
    monkeypatch.setattr(catalog_generation, "submit", lambda ids: [catalog_generation.run_item(i) for i in ids])
    monkeypatch.setattr(catalog_publishing, "submit", lambda ids: [catalog_publishing.run_record(i) for i in ids])

    def fake_draft(config, product, images, rules, settings):
        assert images and Path(images[0]["path"]).is_file()
        suffix = "改" if "修改意见" in config["instructions"] else ""
        return ({"title": "宣传标题" + suffix, "description": "宣传正文", "category": "白酒",
                 "listing_title": "上架标题", "listing_description": "上架描述", "classifications": []}, "test-model")

    def fake_video(images, title, body, directory, settings, *, voice=True, music=False, ai_clips=None,
                   narration_override=None):
        assert images and title
        rendered.append({"ai_clips": ai_clips, "script": narration_override})
        target = directory / "video.mp4"
        target.write_bytes(b"0" * 2000)
        return catalog_video.VideoResult(target, 9.0, voice, False, [] if voice else ["未配音"])

    def fake_images(config, sources, draft, directory, settings, provider=None):
        target = directory / "generated-01.jpg"
        Image.new("RGB", (32, 32), "blue").save(target)
        return [str(target)], "test-image-model"

    monkeypatch.setattr(automation_content, "generate_draft", fake_draft)
    rendered = []
    monkeypatch.setattr(catalog_video, "render_product_video", fake_video)
    monkeypatch.setattr(automation_content, "generate_promotion_images", fake_images)
    published = []

    def fake_execute(platform, operation, payload, connection, *, execution_mode, before_submit):
        published.append((platform, payload))
        before_submit()
        return {"status": "published", "url": f"https://example.com/{platform}", "external_id": "1",
                "account_label": "本机账号"}

    monkeypatch.setattr(automation_executor, "execute", fake_execute)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_identity] = lambda: ALICE
    app.dependency_overrides[routes.require_publisher] = lambda: ALICE
    with TestClient(app) as client:
        client.rendered = rendered
        yield client, published, factory


def create_product(client, name="臻品雅酿", price="499.00"):
    response = client.post(f"{BASE}/products", headers=HEADERS, json={
        "name": name, "price": price, "category": "白酒",
        "specs": [{"name": "规格", "value": "500ml"}, {"name": "酒精度", "value": "53%vol"}],
    })
    assert response.status_code == 201, response.text
    product = response.json()
    response = client.post(f"{BASE}/products/{product['id']}/images", headers=HEADERS,
                           files=[("files", ("a.png", png_bytes(), "image/png"))])
    assert response.status_code == 200, response.text
    return response.json()


def test_product_crud_and_images(catalog):
    client, _, _ = catalog
    product = create_product(client)
    assert product["price"] == "499.00"
    assert len(product["images"]) == 1
    listed = client.get(f"{BASE}/products", params={"q": "臻品"}).json()
    assert listed["total"] == 1 and listed["items"][0]["specs"][0] == {"name": "规格", "value": "500ml"}
    image_id = product["images"][0]["id"]
    thumb = client.get(f"{BASE}/products/{product['id']}/images/{image_id}", params={"size": "thumb"})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    updated = client.put(f"{BASE}/products/{product['id']}", headers=HEADERS,
                         json={"name": "臻品雅酿 2", "price": "520", "specs": []}).json()
    assert updated["price"] == "520.00" and updated["specs"] == []
    assert client.delete(f"{BASE}/products/{product['id']}", headers=HEADERS).status_code == 204
    assert client.get(f"{BASE}/products").json()["total"] == 0


def test_batch_requires_images_and_an_option(catalog):
    client, _, _ = catalog
    bare = client.post(f"{BASE}/products", headers=HEADERS, json={"name": "无图商品"}).json()
    response = client.post(f"{BASE}/batches", headers=HEADERS, json={"product_ids": [bare["id"]]})
    assert response.status_code == 422 and "没有图片" in response.json()["detail"]
    product = create_product(client)
    response = client.post(f"{BASE}/batches", headers=HEADERS, json={
        "product_ids": [product["id"]], "with_copy": False, "with_images": False, "with_video": False})
    assert response.status_code == 422


def test_generate_review_approve_and_publish(catalog):
    client, published, _ = catalog
    first, second = create_product(client, "商品一"), create_product(client, "商品二", price="88")
    batch = client.post(f"{BASE}/batches", headers=HEADERS, json={
        "product_ids": [first["id"], second["id"]], "with_copy": True, "with_images": True, "with_video": True,
    }).json()
    assert batch["total"] == 2
    items = client.get(f"{BASE}/items", params={"status": "review"}).json()
    assert items["total"] == 2
    item = items["items"][0]
    assert item["title"] == "宣传标题" and item["revision"] == 1
    assert {media["kind"] for media in item["media"]} == {"image", "video"}
    media = client.get(f"{BASE}/items/{item['id']}/media/{item['media'][0]['id']}")
    assert media.status_code == 200

    # Editing copy makes the captioned video stale; approval waits for a re-render.
    edited = client.put(f"{BASE}/items/{item['id']}", headers=HEADERS, json={
        "title": "人工标题", "body": "人工正文", "listing_title": "上架标题", "listing_description": "上架描述",
        "category": "白酒"}).json()
    assert edited["video_stale"] is True
    blocked = client.post(f"{BASE}/items/{item['id']}/approve", headers=HEADERS, json={"revision": 1})
    assert blocked.status_code == 409
    regenerated = client.post(f"{BASE}/items/{item['id']}/regenerate", headers=HEADERS,
                              json={"parts": ["copy"], "feedback": "更口语化"})
    assert regenerated.status_code == 202
    refreshed = client.get(f"{BASE}/items/{item['id']}").json()
    assert refreshed["status"] == "review" and refreshed["revision"] == 2
    assert refreshed["title"] == "宣传标题改" and refreshed["video_stale"] is False

    stale = client.post(f"{BASE}/items/{item['id']}/approve", headers=HEADERS, json={"revision": 1})
    assert stale.status_code == 409
    other = [value for value in items["items"] if value["id"] != item["id"]][0]
    bulk = client.post(f"{BASE}/items/approve", headers=HEADERS, json={"ids": [item["id"], other["id"]]}).json()
    assert len(bulk["approved"]) == 2 and not bulk["skipped"]
    contents = client.get(f"{BASE}/contents").json()
    assert contents["total"] == 2 and all(content["has_video"] for content in contents["items"])

    ids = [content["id"] for content in contents["items"]]
    listing = client.post(f"{BASE}/publish/listing", headers=HEADERS, json={
        "content_ids": ids, "location": "贵阳市", "condition": "全新", "shipping_fee": "0"})
    assert listing.status_code == 202, listing.text
    xianyu = [payload for platform, payload in published if platform == "xianyu"]
    assert len(xianyu) == 2 and all(payload["category"] == "其他闲置" for payload in xianyu) and len(xianyu[0]["image_paths"]) == 1
    assert {payload["price"] for payload in xianyu} == {"88.00", "499.00"}

    promotion = client.post(f"{BASE}/publish/promotion", headers=HEADERS,
                            json={"content_ids": ids[:1], "platforms": ["douyin", "kuaishou"]})
    assert promotion.status_code == 202, promotion.text
    records = client.get(f"{BASE}/records").json()
    assert records["total"] == 4 and records["counts"] == {"succeeded": 4}
    assert all(row["external_url"].startswith("https://example.com/") for row in records["items"])
    summary = client.get(f"{BASE}/contents").json()["items"]
    assert any(len(content["publishes"]) == 3 for content in summary)


def test_publish_failures_are_recorded(catalog, monkeypatch):
    client, _, factory = catalog
    product = create_product(client, price=None)
    client.post(f"{BASE}/batches", headers=HEADERS, json={"product_ids": [product["id"]], "with_copy": True})
    item = client.get(f"{BASE}/items").json()["items"][0]
    client.post(f"{BASE}/items/{item['id']}/approve", headers=HEADERS, json={"revision": 1})
    content = client.get(f"{BASE}/contents").json()["items"][0]
    missing_price = client.post(f"{BASE}/publish/listing", headers=HEADERS,
                                json={"content_ids": [content["id"]], "location": "贵阳市"})
    assert missing_price.status_code == 422 and "售价" in missing_price.json()["detail"]
    no_video = client.post(f"{BASE}/publish/promotion", headers=HEADERS,
                           json={"content_ids": [content["id"]], "platforms": ["douyin"]})
    assert no_video.status_code == 422 and "视频" in no_video.json()["detail"]

    def before_click(*args, **kwargs):
        raise HTTPException(503, "浏览器未登录")

    monkeypatch.setattr(automation_executor, "execute", before_click)
    client.post(f"{BASE}/publish/listing", headers=HEADERS, json={
        "content_ids": [content["id"]], "location": "贵阳市", "prices": {content["id"]: "10"}})
    record = client.get(f"{BASE}/records").json()["items"][0]
    assert record["status"] == "failed" and record["error"] == "浏览器未登录"

    def after_click(platform, operation, payload, connection, *, execution_mode, before_submit):
        before_submit()
        raise RuntimeError("timeout")

    monkeypatch.setattr(automation_executor, "execute", after_click)
    assert client.post(f"{BASE}/records/{record['id']}/retry", headers=HEADERS).status_code == 202
    record = client.get(f"{BASE}/records").json()["items"][0]
    assert record["status"] == "unknown" and record["attempt"] == 2
    assert client.post(f"{BASE}/records/{record['id']}/retry", headers=HEADERS).status_code == 409
    resolved = client.post(f"{BASE}/records/{record['id']}/resolve", headers=HEADERS,
                           json={"status": "succeeded", "external_url": "https://www.goofish.com/item?id=1"}).json()
    assert resolved["status"] == "succeeded"


def test_image_failure_keeps_generated_copy(catalog, monkeypatch):
    from content_service.llm_adapters import ProviderCallError

    client, _, _ = catalog
    product = create_product(client)

    def down(*args, **kwargs):
        raise ProviderCallError("已依次尝试 gpt-image-2、gpt-image-1.5，均未生成成功", code="provider_unavailable")

    monkeypatch.setattr(automation_content, "generate_promotion_images", down)
    client.post(f"{BASE}/batches", headers=HEADERS, json={
        "product_ids": [product["id"]], "with_copy": True, "with_images": True, "with_video": True,
        "image_model": "gpt-image-2"})
    item = client.get(f"{BASE}/items").json()["items"][0]
    assert item["status"] == "failed" and "gpt-image-2" in item["error"]
    assert item["title"] == "宣传标题" and item["pending_parts"] == ["images", "video"]
    batch = client.get(f"{BASE}/batches").json()["items"][0]
    assert batch["image_model"] == "gpt-image-2"


def test_video_without_narration_is_noted_for_the_reviewer(catalog):
    client, _, _ = catalog
    product = create_product(client)
    client.post(f"{BASE}/batches", headers=HEADERS, json={
        "product_ids": [product["id"]], "with_copy": True, "with_video": True, "video_voice": False})
    item = client.get(f"{BASE}/items").json()["items"][0]
    assert item["status"] == "review" and item["video_note"] == "未配音"
    assert item["options"]["voice"] is False and item["options"]["music"] is False


def test_narration_text_drops_hashtags_and_emoji():
    from content_service.catalog_video import narration_text

    assert narration_text("标题", "正文内容 #白酒 #送礼 🍶好喝") == "标题。正文内容 好喝"


def test_render_product_video_with_real_ffmpeg(tmp_path):
    from content_service.catalog_video import HEIGHT, WIDTH, render_product_video
    from content_service.provider_dependencies import get_runtime_settings

    images = []
    for index, color in enumerate(("red", "navy")):
        path = tmp_path / f"photo-{index}.jpg"
        Image.new("RGB", (400, 400), color).save(path)
        images.append(path)
    result = render_product_video(images, "测试标题｜第二行", "正文", tmp_path, get_runtime_settings(), voice=False)
    assert result.path.is_file() and result.path.stat().st_size > 1000
    assert not result.narrated and result.duration >= 9 - 0.6
    assert (WIDTH, HEIGHT) == (720, 1280)


def test_ai_video_mode_uses_clips_and_short_script(catalog, monkeypatch):
    from content_service import catalog_ai_video

    client, _, _ = catalog
    product = create_product(client)
    calls = {}

    def fake_clips(images, name, instructions, count, directory):
        calls.update(count=count, first=images[0])
        clip = directory / "ai-clip-01.mp4"
        clip.write_bytes(b"0" * 100)
        return catalog_ai_video.ClipResult([clip], ["第 2 段镜头生成失败：busy"], "seedance-fast")

    monkeypatch.setattr(catalog_ai_video, "generate_ai_clips", fake_clips)
    monkeypatch.setattr(catalog_ai_video, "short_voiceover", lambda title, body, rules: "十五秒口播稿")
    client.post(f"{BASE}/batches", headers=HEADERS, json={
        "product_ids": [product["id"]], "with_copy": True, "with_video": True, "video_mode": "ai", "ai_clip_count": 2})
    item = client.get(f"{BASE}/items").json()["items"][0]
    assert item["status"] == "review" and item["options"]["ai_video"] is True
    assert calls["count"] == 2 and item["video_model"] == "seedance-fast（1 段 AI 镜头）"
    assert "第 2 段" in item["video_note"]
    assert client.rendered[-1]["script"] == "十五秒口播稿" and len(client.rendered[-1]["ai_clips"]) == 1


def test_ai_video_mode_falls_back_to_photos_when_no_clip_works(catalog, monkeypatch):
    from content_service import catalog_ai_video

    client, _, _ = catalog
    product = create_product(client)
    monkeypatch.setattr(catalog_ai_video, "generate_ai_clips",
                        lambda *args: catalog_ai_video.ClipResult([], ["AI 视频服务不可用（denied）"], ""))
    client.post(f"{BASE}/batches", headers=HEADERS, json={
        "product_ids": [product["id"]], "with_copy": True, "with_video": True, "video_mode": "ai"})
    item = client.get(f"{BASE}/items").json()["items"][0]
    assert item["status"] == "review" and item["video_model"] == ""
    assert "改用图片动画" in item["video_note"] and client.rendered[-1]["ai_clips"] is None


def test_generate_ai_clips_keeps_successes_and_reports_failures(tmp_path, monkeypatch):
    from content_service import catalog_ai_video, media_adapters
    from content_service.providers import AiVideoTask, ProviderStatus

    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 64), "red").save(photo)
    submitted = []

    class Port:
        status = ProviderStatus("ark", "seedance", "ai-video-generation", "async-api", True, True, "seedance-fast")

        def submit(self, request):
            assert request.first_frame and request.ratio == "9:16" and request.image_url.startswith("data:image/jpeg")
            submitted.append(request.prompt)
            return AiVideoTask(f"task-{len(submitted)}", "queued", "ark", "seedance-fast")

        def get(self, task_id):
            if task_id == "task-2":
                return AiVideoTask(task_id, "failed", "ark", "seedance-fast", error_message="busy")
            return AiVideoTask(task_id, "succeeded", "ark", "seedance-fast", video_url="https://cdn.example/v.mp4")

    def fake_download(task, *, asset_store, storage_key):
        asset_store.path_for(storage_key).write_bytes(b"clip")

    monkeypatch.setattr(media_adapters, "download_generated_video", fake_download)
    result = catalog_ai_video.generate_ai_clips([photo], "商品", "", 3, tmp_path, port=Port(), sleep=lambda _: None)
    assert [path.name for path in result.paths] == ["ai-clip-01.mp4", "ai-clip-03.mp4"]
    assert result.model == "seedance-fast" and result.notes == ["第 2 段镜头生成失败：busy"]
    assert len(submitted) == 3 and len(set(submitted)) == 3


def test_ark_adapter_uses_first_frame_only_when_asked(monkeypatch):
    import content_service.media_adapters as adapters
    from content_service.providers import AiVideoGenerationRequest

    sent = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "cgt-1", "status": "queued"}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers, json):
            sent.append(json)
            return Response()

    monkeypatch.setattr(adapters.httpx, "Client", Client)
    port = adapters.VolcengineArkAiVideoGenerationPort(provider_id="ark", base_url="https://ark.example/api/v3",
                                                       api_key="k", model="seedance", timeout_seconds=10)
    port.submit(AiVideoGenerationRequest(prompt="p", image_url="https://a.example/1.png", first_frame=True))
    port.submit(AiVideoGenerationRequest(prompt="p", image_url="https://a.example/1.png"))
    assert sent[0]["content"][1]["role"] == "first_frame"
    assert sent[1]["content"][1]["role"] == "reference_image"


def test_render_product_video_with_ai_clip_and_real_ffmpeg(tmp_path):
    import subprocess

    import imageio_ffmpeg

    from content_service.catalog_video import render_product_video
    from content_service.provider_dependencies import get_runtime_settings

    clip = tmp_path / "ai.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=720x1280:rate=25:duration=2", "-pix_fmt", "yuv420p", str(clip)], check=True)
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (300, 300), "red").save(photo)
    result = render_product_video([photo], "标题", "正文", tmp_path, get_runtime_settings(), voice=False,
                                  ai_clips=[clip])
    assert result.path.is_file() and result.path.stat().st_size > 1000 and 1.5 <= result.duration <= 2.5
