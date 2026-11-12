from dataclasses import replace

import pytest

from content_service.config import load_settings
from content_service.knowledge import LibraryKnowledgeProvider
from content_service.knowledge_ingestion import LocalKnowledgeCatalog
from content_service.llm_adapters import (
    FailoverTextProvider,
    OpenAICompatibleTextProvider,
    VolcengineArkTextGenerationPort,
)
from content_service.media_adapters import (
    FfmpegVideoComposerPort,
    LocalAssetStore,
    LocalSubtitleAlignmentPort,
    MockAiVideoGenerationPort,
    MockImageGenerationPort,
    MockMusicGenerationPort,
    MockSpeechSynthesisPort,
)
from content_service.provider_dependencies import (
    get_ai_video_generation_port,
    get_asset_store_port,
    get_image_generation_port,
    get_knowledge_catalog_port,
    get_knowledge_provider,
    get_music_generation_port,
    get_provider_media_publisher,
    get_provider_media_url_signer,
    get_runtime_settings,
    get_speech_synthesis_port,
    get_subtitle_alignment_port,
    get_text_provider,
    get_timeline_renderer,
    get_video_composer_port,
)
from content_service.provider_media_publishers import DisabledProviderMediaPublisher
from content_service.providers import MockTextProvider
from content_service.timeline_media import FfmpegTimelineRenderer


def _clear_factory_caches() -> None:
    for factory in (
        get_runtime_settings,
        get_text_provider,
        get_image_generation_port,
        get_speech_synthesis_port,
        get_music_generation_port,
        get_subtitle_alignment_port,
        get_ai_video_generation_port,
        get_video_composer_port,
        get_timeline_renderer,
        get_provider_media_url_signer,
        get_asset_store_port,
        get_provider_media_publisher,
        get_knowledge_catalog_port,
    ):
        factory.cache_clear()


@pytest.fixture(autouse=True)
def _reset_factories():
    _clear_factory_caches()
    yield
    _clear_factory_caches()


def test_mock_factories_stay_on_local_adapters():
    assert isinstance(get_text_provider(), MockTextProvider)
    assert isinstance(get_image_generation_port(), MockImageGenerationPort)
    assert isinstance(get_speech_synthesis_port(), MockSpeechSynthesisPort)
    assert isinstance(get_music_generation_port(), MockMusicGenerationPort)
    assert isinstance(get_ai_video_generation_port(), MockAiVideoGenerationPort)
    assert isinstance(get_subtitle_alignment_port(), LocalSubtitleAlignmentPort)
    assert isinstance(get_video_composer_port(), FfmpegVideoComposerPort)
    assert isinstance(get_timeline_renderer(), FfmpegTimelineRenderer)
    assert isinstance(get_asset_store_port(), LocalAssetStore)
    assert isinstance(get_provider_media_publisher(), DisabledProviderMediaPublisher)
    get_provider_media_url_signer()
    assert isinstance(get_knowledge_catalog_port(), LocalKnowledgeCatalog)
    assert isinstance(get_knowledge_provider(session=None), LibraryKnowledgeProvider)


def test_incomplete_external_factories_fail_closed(monkeypatch):
    from content_service import provider_dependencies as dependencies

    base = load_settings()
    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            llm_provider="openai_compatible",
            llm_base_url=None,
            llm_api_key=None,
        ),
    )
    with pytest.raises(RuntimeError, match="内容模型配置不完整"):
        get_text_provider()

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base, image_provider="volcengine_ark", image_base_url=None, image_api_key=None
        ),
    )
    get_image_generation_port.cache_clear()
    with pytest.raises(RuntimeError, match="图片模型配置不完整"):
        get_image_generation_port()

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            speech_provider="volcengine",
            speech_base_url=None,
            speech_app_id=None,
            speech_access_token=None,
        ),
    )
    get_speech_synthesis_port.cache_clear()
    with pytest.raises(RuntimeError, match="配音服务配置不完整"):
        get_speech_synthesis_port()

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            music_provider="minimax",
            music_base_url=None,
            music_api_key=None,
        ),
    )
    get_music_generation_port.cache_clear()
    with pytest.raises(RuntimeError, match="配乐服务配置不完整"):
        get_music_generation_port()

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            ai_video_provider="volcengine_ark",
            ai_video_base_url=None,
            ai_video_api_key=None,
        ),
    )
    get_ai_video_generation_port.cache_clear()
    with pytest.raises(RuntimeError, match="AI 视频模型配置不完整"):
        get_ai_video_generation_port()

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(base, knowledge_provider="jsonl", knowledge_dir=None),
    )
    with pytest.raises(RuntimeError, match="知识库目录未配置"):
        get_knowledge_provider(session=None)


def test_configured_text_and_media_factories(monkeypatch):
    from content_service import provider_dependencies as dependencies

    base = load_settings()
    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            llm_provider="openai_compatible",
            llm_base_url="https://model.example/v1",
            llm_api_key="secret",
            llm_fallback_model=None,
        ),
    )
    get_text_provider.cache_clear()
    from content_service.provider_health import HealthTrackedProvider

    tracked = get_text_provider()
    assert isinstance(tracked, HealthTrackedProvider)
    assert isinstance(tracked._provider, OpenAICompatibleTextProvider)

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            llm_provider="volcengine_ark",
            llm_base_url="https://ark.example/api/v3",
            llm_api_key="secret",
            llm_fallback_model="backup-model",
        ),
    )
    get_text_provider.cache_clear()
    provider = get_text_provider()
    assert isinstance(provider, HealthTrackedProvider)
    assert isinstance(provider._provider, FailoverTextProvider)
    assert isinstance(provider._provider.primary, VolcengineArkTextGenerationPort)

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            speech_provider="chat-tts",
            speech_base_url="https://tts.example",
            speech_api_key="secret",
        ),
    )
    get_speech_synthesis_port.cache_clear()
    from content_service.media_adapters import OpenAICompatibleSpeechSynthesisPort

    speech = get_speech_synthesis_port()
    assert isinstance(speech, HealthTrackedProvider)
    assert isinstance(speech._provider, OpenAICompatibleSpeechSynthesisPort)

    monkeypatch.setattr(
        dependencies,
        "get_runtime_settings",
        lambda: replace(
            base,
            speech_provider="volcengine_tts_api_key",
            speech_base_url="https://openspeech.example",
            speech_api_key="secret",
        ),
    )
    get_speech_synthesis_port.cache_clear()
    from content_service.media_adapters import VolcengineApiKeySpeechSynthesisPort

    api_key_speech = get_speech_synthesis_port()
    assert isinstance(api_key_speech, HealthTrackedProvider)
    assert isinstance(api_key_speech._provider, VolcengineApiKeySpeechSynthesisPort)
