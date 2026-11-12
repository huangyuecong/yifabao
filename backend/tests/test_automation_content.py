"""Exercise source isolation, structured vision output and an actual encoded video."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent_core import Identity
from content_service import automation_content as content
from content_service.db import Base
from content_service.llm_adapters import ProviderCallError
from content_service.media_adapters import LocalAssetStore
from content_service.models import AssetRef, LibraryItem, ResourceEntry


@pytest.fixture
def source_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(content, "SessionLocal", factory)
    objects = tmp_path / "objects"
    objects.mkdir()
    monkeypatch.setattr(content, "get_asset_store_port", lambda: LocalAssetStore(objects))
    Image.new("RGB", (200, 160), "red").save(objects / "photo.png")
    with factory() as session:
        session.add(
            ResourceEntry(
                id="product", owner_id="owner", resource_kind="product", title="木制收纳盒"
            )
        )
        session.add(
            ResourceEntry(
                id="photo",
                owner_id="owner",
                resource_kind="image",
                title="主图",
                parent_id="product",
            )
        )
        session.add(
            AssetRef(
                id="asset",
                entry_id="photo",
                storage_key="photo.png",
                media_type="image/png",
                size_bytes=100,
                checksum="sha",
            )
        )
        session.add(
            LibraryItem(
                id="product-item",
                owner_id="owner",
                category="product",
                name="木制收纳盒",
                text_content="收纳使用",
                resource_entry_id="product",
            )
        )
        session.add(
            LibraryItem(
                id="image-item",
                owner_id="owner",
                category="image",
                name="主图",
                storage_key="photo.png",
                resource_entry_id="photo",
            )
        )
        session.commit()
    yield factory, SimpleNamespace(data_dir=tmp_path, ffmpeg_path=None, ffmpeg_timeout_seconds=120)
    engine.dispose()


def identity(user="owner"):
    return Identity(user, user, "", frozenset({"user"}))


def config(**changes):
    return {
        "execution_mode": "browser",
        "product_id": "product-item",
        "image_ids": [],
        "platforms": ["xianyu"],
        "video_mode": "slideshow",
        "price": "25.00",
        "location": "上海",
        "condition": "九成新",
        "shipping_fee": "6.00",
        **changes,
    }


def result(image_id="photo"):
    return {
        "title": "木制收纳盒",
        "description": "木制收纳盒，适合整理桌面。",
        "category": "收纳用品",
        "classifications": [
            {"id": image_id, "role": "main", "reason": "正面全景", "confidence": 0.9}
        ],
    }


def test_product_resolves_related_images_without_mutating_library(source_db):
    factory, settings = source_db
    product, images, video, _ = content.load_sources(config(), identity(), settings)
    assert product["name"] == "木制收纳盒"
    assert [im["id"] for im in images] == ["photo"]
    assert video is None
    with factory() as session:
        assert session.get(LibraryItem, "image-item").category == "image"


def test_other_owner_cannot_read_private_source(source_db):
    _, settings = source_db
    with pytest.raises(HTTPException) as exc:
        content.load_sources(config(), identity("intruder"), settings)
    assert exc.value.status_code == 403


def test_source_path_traversal_is_rejected(source_db):
    factory, settings = source_db
    with factory() as session:
        session.get(LibraryItem, "image-item").storage_key = "../secret.txt"
        session.commit()
    with pytest.raises(HTTPException) as exc:
        content.load_sources(config(image_ids=["image-item"]), identity(), settings)
    assert exc.value.status_code == 422


def test_authorised_media_uses_shared_archive_store(source_db, monkeypatch):
    _, settings = source_db
    original = settings.data_dir / "objects" / "photo.png"
    original.unlink()
    restored = []

    class RestoringStore:
        def path_for(self, key):
            restored.append(key)
            Image.new("RGB", (120, 120), "blue").save(original)
            return original

    monkeypatch.setattr(content, "get_asset_store_port", lambda: RestoringStore())
    _, images, _, _ = content.load_sources(config(), identity(), settings)
    assert restored == ["photo.png"]
    assert images[0]["path"].is_file()


def test_visual_output_requires_every_image_and_known_roles():
    draft = result()
    draft["classifications"][0]["id"] = "invented"
    with pytest.raises(ProviderCallError):
        content._decode_draft(json.dumps(draft), [{"id": "photo"}], [])
    draft = result()
    draft["classifications"][0]["confidence"] = float("nan")
    with pytest.raises(ProviderCallError):
        content._decode_draft(json.dumps(draft), [{"id": "photo"}], [])


def test_existing_compliance_rules_block_generated_copy():
    with pytest.raises(HTTPException) as exc:
        content._decode_draft(json.dumps(result()), [{"id": "photo"}], ["整理桌面"])
    assert exc.value.status_code == 422


def test_vision_request_contains_images_and_facts_but_not_paths(source_db, monkeypatch):
    _, settings = source_db
    product, images, _, rules = content.load_sources(config(), identity(), settings)
    model = SimpleNamespace(llm_provider="openai_compatible", llm_model="vision-model")
    monkeypatch.setattr(content, "model_settings", lambda *_: model)
    calls = []

    def call(*args):
        calls.append(args[2])
        return json.dumps(result())

    monkeypatch.setattr(content, "call_chat", call)
    draft, chosen_model = content.generate_draft(config(), product, images, rules, settings)
    assert chosen_model == "vision-model"
    assert draft["classifications"][0]["label"] == "商品主图"
    serialized = json.dumps(calls, ensure_ascii=False)
    assert "data:image/jpeg;base64," in serialized
    assert str(settings.data_dir) not in serialized
    assert "25.00" in serialized and "九成新" in serialized


def test_prepare_snapshots_files_and_preserves_explicit_sale_terms(source_db, monkeypatch):
    _, settings = source_db
    monkeypatch.setattr(content, "get_runtime_settings", lambda: settings)
    monkeypatch.setattr(content, "generate_draft", lambda *args: (result(), "vision-model"))
    prepared = content.prepare_content(config(platforms=["xianyu"]), identity())
    assert prepared["price"] == "25.00"
    assert prepared["shipping_fee"] == "6.00"
    assert prepared["condition"] == "九成新"
    snapshot = Path(prepared["image_paths"][0])
    assert snapshot.is_relative_to(settings.data_dir / "automation-media")
    (settings.data_dir / "objects" / "photo.png").unlink()
    assert snapshot.is_file()


def test_slideshow_is_a_decodable_h264_video(source_db, tmp_path):
    import imageio_ffmpeg

    _, settings = source_db
    folder = tmp_path / "render"
    folder.mkdir()
    video = content.render_slideshow(
        [settings.data_dir / "objects" / "photo.png"], result(), folder, settings
    )
    assert video.stat().st_size > 1000
    decoded = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-v",
            "error",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=30,
    )
    assert decoded.returncode == 0


def custom_config(**changes):
    return config(
        source_mode="custom",
        custom_product_name="自定义玻璃茶杯",
        custom_product_description="玻璃材质，容量 200 毫升，无包装，无磕碰。",
        product_id="missing-stale-product",
        image_ids=["image-item"],
        **changes,
    )


def test_custom_product_ignores_stale_product_and_uses_selected_authorised_images(source_db):
    _, settings = source_db
    product, images, video, _ = content.load_sources(custom_config(), identity(), settings)
    assert product["name"] == "自定义玻璃茶杯"
    assert product["text"] == "玻璃材质，容量 200 毫升，无包装，无磕碰。"
    assert product["entry_id"] is None
    assert [image["id"] for image in images] == ["image-item"]
    assert video is None


def test_custom_product_does_not_bypass_image_owner_access(source_db):
    _, settings = source_db
    with pytest.raises(HTTPException) as error:
        content.load_sources(custom_config(), identity("intruder"), settings)
    assert error.value.status_code == 403


def test_custom_facts_are_sent_to_model_and_sale_terms_remain_explicit(source_db, monkeypatch):
    _, settings = source_db
    chosen = custom_config(platforms=["xianyu"])
    monkeypatch.setattr(content, "get_runtime_settings", lambda: settings)
    model = SimpleNamespace(llm_provider="openai_compatible", llm_model="vision-model")
    monkeypatch.setattr(content, "model_settings", lambda *_: model)
    captured = []

    def call(*args):
        facts = json.loads(args[2][1]["content"][0]["text"])
        captured.append(facts)
        generated = result("image-item") | {
            "title": "玻璃茶杯",
            "description": "200 毫升玻璃茶杯，无包装。",
        }
        return json.dumps(generated)

    monkeypatch.setattr(content, "call_chat", call)
    prepared = content.prepare_content(chosen, identity())
    facts = captured[0]
    assert facts["source_mode"] == "custom"
    assert facts["product"]["name"] == chosen["custom_product_name"]
    assert facts["product"]["text"] == chosen["custom_product_description"]
    assert "木制收纳盒" not in json.dumps(facts, ensure_ascii=False)
    assert prepared["source_summary"]["product_name"] == chosen["custom_product_name"]
    assert prepared["price"] == "25.00"
    assert prepared["condition"] == "九成新"
    assert prepared["shipping_fee"] == "6.00"
    assert prepared["location"] == "上海"
    assert Path(prepared["image_paths"][0]).is_file()


def test_library_source_ignores_residual_custom_facts(source_db):
    _, settings = source_db
    product, images, _, _ = content.load_sources(
        config(
            source_mode="library",
            custom_product_name="不应使用",
            custom_product_description="不应使用",
        ),
        identity(),
        settings,
    )
    assert product["name"] == "木制收纳盒"
    assert [image["id"] for image in images] == ["photo"]


def test_listing_and_promotion_have_separate_prompts_and_transaction_rules(source_db, monkeypatch):
    _, settings = source_db
    product, images, _, rules = content.load_sources(config(), identity(), settings)
    model = SimpleNamespace(llm_provider="openai_compatible", llm_model="vision-model")
    monkeypatch.setattr(content, "model_settings", lambda *_: model)
    calls = []

    def call(*args):
        calls.append(args[2])
        return json.dumps(result())

    monkeypatch.setattr(content, "call_chat", call)
    content.generate_draft(config(), product, images, rules, settings)
    content.generate_draft(config(platforms=["douyin"]), product, images, rules, settings)
    listing = json.loads(calls[0][1]["content"][0]["text"])
    promotion = json.loads(calls[1][1]["content"][0]["text"])
    assert listing["purpose"] == "listing" and promotion["purpose"] == "promotion"
    assert listing["platforms"] == ["xianyu"] and promotion["platforms"] == ["douyin"]
    assert listing["rules"]["price"] == "25.00"
    assert not set(promotion["rules"]) & {"price", "condition", "shipping_fee", "location"}
    assert "本次任务仅为闲鱼商品上架" in calls[0][0]["content"]
    assert "本次任务仅为抖音视频宣传" in calls[1][0]["content"]
    assert "25.00" not in json.dumps(promotion) and "上海" not in json.dumps(
        promotion, ensure_ascii=False
    )


def test_promotion_output_contains_no_transaction_fields(source_db, monkeypatch):
    _, settings = source_db
    monkeypatch.setattr(content, "get_runtime_settings", lambda: settings)
    monkeypatch.setattr(content, "generate_draft", lambda *args: (result(), "vision-model"))

    def render(images, draft, directory, settings):
        path = directory / "video.mp4"
        path.write_bytes(b"fixture")
        return path

    monkeypatch.setattr(content, "render_slideshow", render)
    prepared = content.prepare_content(config(platforms=["douyin"]), identity())
    assert not set(prepared) & {"price", "condition", "shipping_fee", "location"}
    assert prepared["video_path"]


@pytest.mark.parametrize(
    "platforms", [["xiaohongshu"], ["kuaishou"], ["douyin", "xiaohongshu", "kuaishou"]]
)
def test_registered_promotion_platforms_prepare_video(source_db, monkeypatch, platforms):
    _, settings = source_db
    monkeypatch.setattr(content, "get_runtime_settings", lambda: settings)
    monkeypatch.setattr(content, "generate_draft", lambda *args: (result(), "vision-model"))
    renders = []

    def render(images, draft, directory, settings):
        renders.append(list(images))
        path = directory / "video.mp4"
        path.write_bytes(b"fixture")
        return path

    monkeypatch.setattr(content, "render_slideshow", render)
    prepared = content.prepare_content(
        config(platforms=platforms, execution_mode="browser"), identity()
    )
    assert len(renders) == 1 and Path(prepared["video_path"]).is_file()
    assert not set(prepared) & {"price", "condition", "shipping_fee", "location"}


@pytest.mark.parametrize("platform", ["xiaohongshu", "kuaishou"])
def test_all_video_platforms_validate_existing_video_sources(source_db, platform):
    _, settings = source_db
    with pytest.raises(HTTPException, match="请选择可上传"):
        content.load_sources(
            config(platforms=[platform], execution_mode="browser", video_mode="existing"),
            identity(),
            settings,
        )


def test_multi_platform_prompt_and_model_output_obey_strictest_limits(source_db, monkeypatch):
    _, settings = source_db
    selected = config(platforms=["douyin", "xiaohongshu", "kuaishou"], execution_mode="browser")
    product, images, _, rules = content.load_sources(selected, identity(), settings)
    monkeypatch.setattr(
        content,
        "model_settings",
        lambda *_: SimpleNamespace(llm_provider="openai_compatible", llm_model="vision-model"),
    )
    calls = []

    def call(*args):
        calls.append(args[2])
        return json.dumps(result() | {"title": "字" * 21})

    monkeypatch.setattr(content, "call_chat", call)
    with pytest.raises(ProviderCallError):
        content.generate_draft(selected, product, images, rules, settings)
    system = calls[0][0]["content"]
    assert "抖音、小红书、快手视频宣传" in system
    assert "title(1-20字)" in system
    facts = json.loads(calls[0][1]["content"][0]["text"])
    assert facts["platform_names"] == ["抖音", "小红书", "快手"]
    assert not set(facts["rules"]) & {"price", "condition", "shipping_fee", "location"}


def test_manual_listing_preserves_copy_and_images_without_model_calls(source_db, monkeypatch):
    _, settings = source_db
    monkeypatch.setattr(content, 'get_runtime_settings', lambda: settings)
    monkeypatch.setattr(content, 'generate_draft', lambda *_: pytest.fail('Manual listing must not call AI'))
    prepared = content.prepare_content(config(
        source_mode='custom', copy_mode='manual', custom_product_name='收纳盒',
        custom_product_description='使用过，右侧有划痕。', image_ids=['image-item'],
        category='其他闲置',
    ), identity())
    assert prepared['title'] == '收纳盒'
    assert prepared['description'] == '使用过，右侧有划痕。'
    assert prepared['model'] == 'manual'
    assert prepared['category'] == '其他闲置'
    assert prepared['classifications'][0]['id'] == 'image-item'
    assert Path(prepared['image_paths'][0]).is_file()
    assert prepared['video_path'] is None


def test_generated_images_use_original_references_and_revisions_rebuild_video(source_db, monkeypatch):
    import io
    from content_service import automation_review as review
    from content_service.providers import GeneratedAsset
    _, settings = source_db
    monkeypatch.setattr(content, 'get_runtime_settings', lambda: settings)
    monkeypatch.setattr(review, 'get_runtime_settings', lambda: settings)
    monkeypatch.setattr(content, 'generate_draft', lambda *args: (result(), 'vision-model'))
    requests, renders = [], []
    buffer = io.BytesIO()
    Image.new('RGB', (100, 100), 'blue').save(buffer, 'PNG')
    class Provider:
        status = SimpleNamespace(enabled=True, mode='api', model='image-model')
        def generate(self, request):
            requests.append(request)
            return [GeneratedAsset(buffer.getvalue(), 'image/png', '.png', 'test', 'image-model')]
    monkeypatch.setattr(content, 'get_image_generation_port', lambda: Provider())
    def render(images, draft, directory, settings):
        renders.append((list(images), draft['title']))
        target = directory / 'video.mp4'
        target.write_bytes(b'video')
        return target
    monkeypatch.setattr(content, 'render_slideshow', render)
    cfg = config(platforms=['douyin'], image_mode='generate', generated_image_count=2)
    prepared = review.new_version(content.prepare_content(cfg, identity()))
    assert len(requests) == 2 and len(prepared['image_paths']) == 2
    assert all(request.reference_images and request.reference_media_types == ('image/jpeg',) for request in requests)
    original = Path(prepared['_sources']['images'][0]['path']).read_bytes()
    assert requests[0].reference_images[0] == original
    old_video = prepared['video_path']
    prepared['_revision_request'] = {'feedback': '重新生成', 'scope': 'all'}
    revised = content.revise_content(content.business_config(cfg), prepared, identity())
    assert revised['_review']['revision'] == 2 and revised['_review']['state'] == 'pending'
    assert revised['video_path'] != old_video and Path(old_video).is_file()
    assert requests[2].reference_images[0] == original
    assert renders[-1][0] == revised['image_paths'] and not revised['video_stale']


def test_approved_media_archive_is_idempotent_and_reusable(source_db, monkeypatch):
    from sqlalchemy import func, select
    from content_service import automation_review as review
    from content_service import provider_dependencies as deps
    factory, settings = source_db
    monkeypatch.setattr(content, 'get_runtime_settings', lambda: settings)
    monkeypatch.setattr(review, 'get_runtime_settings', lambda: settings)
    monkeypatch.setattr(deps, 'get_asset_store_port', lambda: LocalAssetStore(settings.data_dir / 'objects'))
    monkeypatch.setattr(content, 'generate_draft', lambda *_: (result(), 'vision-model'))
    cfg = config()
    payload = review.new_version(content.prepare_content(cfg, identity()))
    review.approve(payload, cfg, 'owner', 1)
    run = SimpleNamespace(id='test-run', owner_id='owner')
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(ResourceEntry))
        ids = review.archive_to_library(session, run, payload)
        session.commit()
        assert review.archive_to_library(session, run, payload) == ids
        session.commit()
        assert session.scalar(select(func.count()).select_from(ResourceEntry)) == before + 2
    product, images, _, _ = content.load_sources(config(product_id=None, image_ids=[ids[1]]), identity(), settings)
    assert product is None and images[0]['path'].is_file()
    review.ensure_approved(payload, cfg, force=True)


def test_unavailable_image_channel_reports_safe_reason_and_removes_failed_output(source_db, monkeypatch):
    import httpx
    from content_service import automation_review as review
    _, settings = source_db
    monkeypatch.setattr(content, 'get_runtime_settings', lambda: settings)
    monkeypatch.setattr(review, 'get_runtime_settings', lambda: settings)
    monkeypatch.setattr(content, 'generate_draft', lambda *_: (result(), 'vision-model'))
    class Provider:
        status = SimpleNamespace(enabled=True, mode='api', model='image-model')
        def generate(self, _request):
            response = httpx.Response(502, json={'error': {'message': 'No available compatible accounts'}})
            cause = httpx.HTTPStatusError('upstream unavailable', request=httpx.Request('POST', 'https://example.invalid/images/edits'), response=response)
            raise ProviderCallError('HTTP 502') from cause
    monkeypatch.setattr(content, 'get_image_generation_port', lambda: Provider())
    with pytest.raises(ProviderCallError, match='没有可用的兼容账号'):
        content.prepare_content(config(platforms=['douyin'], image_mode='generate'), identity())
    assert list((settings.data_dir / 'automation-media').iterdir()) == []


def test_retrieved_references_are_sent_with_grounding_rules(source_db, monkeypatch):
    _, settings = source_db
    product, images, _, rules = content.load_sources(config(), identity(), settings)
    model = SimpleNamespace(llm_provider="openai_compatible", llm_model="vision-model")
    monkeypatch.setattr(content, "model_settings", lambda *_: model)
    calls = []

    def call(*args):
        calls.append(args[2])
        return json.dumps(result())

    monkeypatch.setattr(content, "call_chat", call)
    content.generate_draft(config(), product, images, rules, settings)
    referenced = {**product, "references": [{"id": "R1", "title": "木制收纳盒", "text": "宣传语：收纳生活",
                                             "source": "https://example.com/box", "category": "product"}]}
    content.generate_draft(config(), referenced, images, rules, settings)
    plain, grounded = (json.loads(messages[1]["content"][0]["text"]) for messages in calls)
    assert "references" not in plain and "references" not in calls[0][0]["content"]
    assert grounded["references"] == [{"id": "R1", "title": "木制收纳盒", "text": "宣传语：收纳生活"}]
    assert "以商品事实为准" in calls[1][0]["content"] and "资料里没有的数字" in calls[1][0]["content"]
