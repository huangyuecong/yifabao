from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends
from sqlalchemy.orm import Session

from .ai_model_config import model_settings
from .config import Settings, load_settings
from .db import get_session
from .hiagent_adapters import (
    HiAgentClient,
    HiAgentImageGenerationPort,
    HiAgentVideoGenerationPort,
)
from .http_media_relay import HttpMediaRelayPublisher
from .image_fallback import FallbackImageGenerationPort
from .knowledge import JsonlKnowledgeProvider, LibraryKnowledgeProvider
from .knowledge_ingestion import LocalKnowledgeCatalog
from .llm_adapters import (
    FailoverTextProvider,
    OpenAICompatibleTextProvider,
    VolcengineArkTextGenerationPort,
)
from .media_adapters import (
    FfmpegVideoComposerPort,
    LocalAssetStore,
    LocalSubtitleAlignmentPort,
    MiniMaxMusicGenerationPort,
    MockAiVideoGenerationPort,
    MockImageGenerationPort,
    MockMusicGenerationPort,
    MockSpeechSynthesisPort,
    VolcengineApiKeySpeechSynthesisPort,
    VolcengineArkAiVideoGenerationPort,
    VolcengineArkImageGenerationPort,
    VolcengineSpeechSynthesisPort,
)
from .openai_image_adapter import OpenAICompatibleImageGenerationPort
from .provider_health import HealthTrackedProvider, track_provider
from .provider_media_gateway import ProviderMediaUrlSigner
from .provider_media_publishers import (
    DisabledProviderMediaPublisher,
    TosProviderMediaPublisher,
)
from .providers import (
    AiVideoGenerationPort,
    AssetStorePort,
    ImageGenerationPort,
    KnowledgeCatalogPort,
    KnowledgeProvider,
    MockTextProvider,
    MusicGenerationPort,
    ProviderMediaPublisherPort,
    SpeechSynthesisPort,
    SubtitleAlignmentPort,
    TextProvider,
    VideoComposerPort,
)
from .responses_image_adapter import OpenAIResponsesImageGenerationPort
from .timeline_media import FfmpegTimelineRenderer


@lru_cache(maxsize=1)
def get_runtime_settings() -> Settings:
    return load_settings()


def configured_provider(capability):
    def decorate(factory):
        @lru_cache(maxsize=8)
        def cached(settings):
            provider = factory(settings)
            if isinstance(provider, HealthTrackedProvider):
                provider.health_configuration = uuid4().hex
            return provider

        def resolve():
            return cached(model_settings(get_runtime_settings(), capability))

        resolve.__name__ = factory.__name__
        resolve.cache_clear = cached.cache_clear
        resolve.__wrapped__ = lambda: factory(model_settings(get_runtime_settings(), capability))
        return resolve

    return decorate


def hiagent_client(settings, access_key, timeout):
    endpoint = urlsplit(settings.hiagent_base_url)
    return HiAgentClient(
        access_key=access_key or "",
        secret_key=settings.hiagent_secret_key or "",
        workspace_id=settings.hiagent_workspace_id,
        host=endpoint.hostname,
        port=endpoint.port or 443,
        timeout=timeout,
    )


@configured_provider("text")
def get_text_provider(settings: Settings) -> TextProvider:
    if settings.llm_provider == "mock":
        return MockTextProvider()
    if not settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("外部内容模型配置不完整")
    provider_class = (
        VolcengineArkTextGenerationPort
        if settings.llm_provider == "volcengine_ark"
        else OpenAICompatibleTextProvider
    )
    primary = provider_class(
        provider_id=settings.llm_provider_id,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
        max_attempts=settings.llm_max_attempts,
    )
    if not settings.llm_fallback_model:
        return track_provider(primary)
    fallback = provider_class(
        provider_id=settings.llm_provider_id,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_fallback_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
        max_attempts=settings.llm_max_attempts,
    )
    return track_provider(FailoverTextProvider(primary, fallback))


@lru_cache(maxsize=4)
def _jsonl_provider(directory: str, namespace: str) -> JsonlKnowledgeProvider:
    from pathlib import Path

    return JsonlKnowledgeProvider(Path(directory), namespace)


def get_knowledge_provider(
    session: Session = Depends(get_session),
) -> KnowledgeProvider:
    settings = get_runtime_settings()
    if settings.knowledge_provider == "library_db":
        return LibraryKnowledgeProvider(session, settings.knowledge_namespace)
    if settings.knowledge_provider == "weknora":
        from .weknora import WeKnoraKnowledgeProvider

        return track_provider(WeKnoraKnowledgeProvider(
            session, get_weknora_client(), namespace=settings.knowledge_namespace,
            allowed_data_levels=settings.weknora_allowed_data_levels,
        ))
    if settings.knowledge_dir is None:
        raise RuntimeError("JSONL 知识库目录未配置")
    return _jsonl_provider(str(settings.knowledge_dir), settings.knowledge_namespace)


@lru_cache(maxsize=1)
def get_weknora_client():
    from .weknora import WeKnoraClient

    settings = get_runtime_settings()
    return WeKnoraClient(
        settings.weknora_base_url or "", settings.weknora_api_key or "",
        settings.weknora_knowledge_base_id or "", tenant_id=settings.weknora_tenant_id,
        timeout_seconds=settings.weknora_timeout_seconds,
    )


def build_image_generation_port(settings: Settings) -> ImageGenerationPort:
    if settings.image_provider == "mock":
        return MockImageGenerationPort(
            provider_id=settings.image_provider_id,
            model=settings.image_model,
            max_candidates=settings.image_max_candidates,
        )
    if settings.image_provider == "hiagent":
        return track_provider(
            HiAgentImageGenerationPort(
                client=hiagent_client(
                    settings, settings.image_api_key, settings.image_timeout_seconds
                ),
                model_service_id=settings.image_model,
                provider_id=settings.image_provider_id,
                max_candidates=settings.image_max_candidates,
                supported_sizes=settings.image_supported_sizes,
                supported_ratios=settings.image_supported_ratios,
            )
        )
    if not settings.image_base_url or not settings.image_api_key:
        raise RuntimeError("外部图片模型配置不完整")
    provider_class = (
        OpenAICompatibleImageGenerationPort
        if settings.image_provider == "openai_compatible"
        else VolcengineArkImageGenerationPort
    )
    extra = {}
    if settings.image_provider == "openai_responses":
        provider_class = OpenAIResponsesImageGenerationPort
        extra = {"driver_model": settings.image_responses_model}
    models = list(dict.fromkeys([settings.image_model, *settings.image_fallback_models]))
    ports = [
        track_provider(
            provider_class(
                **extra,
                provider_id=settings.image_provider_id,
                base_url=settings.image_base_url,
                api_key=settings.image_api_key,
                model=model,
                timeout_seconds=settings.image_timeout_seconds,
                max_candidates=settings.image_max_candidates,
                supported_sizes=settings.image_supported_sizes,
                supported_ratios=settings.image_supported_ratios,
            )
        )
        for model in models
    ]
    return ports[0] if len(ports) == 1 else FallbackImageGenerationPort(ports)


@configured_provider("image")
def get_image_generation_port(settings: Settings) -> ImageGenerationPort:
    return build_image_generation_port(settings)


def image_port_for_models(models: list[str]) -> ImageGenerationPort:
    """Uncached port that tries exactly these models, in order, with the configured key."""
    settings = model_settings(get_runtime_settings(), "image")
    models = [model for model in dict.fromkeys(models) if model]
    if not models or settings.image_provider in {"mock", "hiagent"}:
        return get_image_generation_port()
    return build_image_generation_port(
        replace(settings, image_model=models[0], image_fallback_models=tuple(models[1:]))
    )


@configured_provider("speech")
def get_speech_synthesis_port(settings: Settings) -> SpeechSynthesisPort:
    if settings.speech_provider == "mock":
        return MockSpeechSynthesisPort(
            provider_id=settings.speech_provider_id,
            default_voice=settings.speech_voice_type,
        )
    if settings.speech_provider == "chat-tts":
        from .media_adapters import OpenAICompatibleSpeechSynthesisPort

        return track_provider(
            OpenAICompatibleSpeechSynthesisPort(
                provider_id=settings.speech_provider_id,
                base_url=settings.speech_base_url or "",
                api_key=settings.speech_api_key or "",
                model=settings.speech_resource_id or "chat-tts",
                default_voice=settings.speech_voice_type,
                timeout_seconds=settings.speech_timeout_seconds,
                supported_voices=settings.speech_supported_voices,
            )
        )
    if settings.speech_provider == "volcengine_tts_api_key":
        if not settings.speech_base_url or not settings.speech_api_key:
            raise RuntimeError("新版外部配音服务配置不完整")
        return track_provider(
            VolcengineApiKeySpeechSynthesisPort(
                provider_id=settings.speech_provider_id,
                base_url=settings.speech_base_url,
                api_key=settings.speech_api_key,
                resource_id=settings.speech_resource_id,
                default_voice=settings.speech_voice_type,
                timeout_seconds=settings.speech_timeout_seconds,
                supported_voices=settings.speech_supported_voices,
            )
        )
    if (
        not settings.speech_base_url
        or not settings.speech_app_id
        or not settings.speech_access_token
    ):
        raise RuntimeError("外部配音服务配置不完整")
    return track_provider(
        VolcengineSpeechSynthesisPort(
            provider_id=settings.speech_provider_id,
            base_url=settings.speech_base_url,
            app_id=settings.speech_app_id,
            access_token=settings.speech_access_token,
            resource_id=settings.speech_resource_id,
            default_voice=settings.speech_voice_type,
            timeout_seconds=settings.speech_timeout_seconds,
            supported_voices=settings.speech_supported_voices,
            cluster=settings.speech_cluster,
        )
    )


@configured_provider("music")
def get_music_generation_port(settings: Settings) -> MusicGenerationPort:
    common = {
        "provider_id": settings.music_provider_id,
        "model": settings.music_model,
        "minimum_duration_seconds": settings.music_min_duration_seconds,
        "maximum_duration_seconds": settings.music_max_duration_seconds,
        "max_bytes": settings.music_max_bytes,
    }
    if settings.music_provider == "mock":
        return MockMusicGenerationPort(**common)
    if settings.music_provider == "seedance":
        from .seedance_music import SeedanceMusicGenerationPort

        return track_provider(
            SeedanceMusicGenerationPort(
                **common,
                base_url=settings.music_base_url or "https://ark.cn-beijing.volces.com/api/v3",
                api_key=settings.music_api_key,
                timeout_seconds=settings.music_timeout_seconds,
                poll_interval_seconds=settings.music_poll_interval_seconds,
                allowed_download_hosts=settings.music_allowed_download_hosts,
                ffmpeg_path=settings.ffmpeg_path,
            )
        )
    if settings.music_provider == "hiagent_seedance":
        from .seedance_music import HiAgentSeedanceMusicGenerationPort

        return track_provider(
            HiAgentSeedanceMusicGenerationPort(
                **common,
                access_key=settings.hiagent_access_key or settings.image_api_key or "",
                secret_key=settings.hiagent_secret_key or "",
                workspace_id=settings.hiagent_workspace_id,
                pro_model=settings.music_model,
                fast_model=settings.hiagent_video_fallback_model or settings.music_model,
                gateway_base_url=settings.hiagent_base_url,
                base_url=settings.music_base_url or "https://ark.cn-beijing.volces.com/api/v3",
                timeout_seconds=settings.music_timeout_seconds,
                poll_interval_seconds=settings.music_poll_interval_seconds,
                allowed_download_hosts=settings.music_allowed_download_hosts,
                ffmpeg_path=settings.ffmpeg_path,
            )
        )
    if settings.music_provider == "volcengine":
        from .volcengine_music import VolcengineMusicGenerationPort

        return track_provider(
            VolcengineMusicGenerationPort(
                **common,
                base_url=settings.music_base_url or "https://open.volcengineapi.com",
                access_key=settings.music_access_key,
                secret_key=settings.music_secret_key,
                billing=settings.music_billing,
                timeout_seconds=settings.music_timeout_seconds,
                poll_interval_seconds=settings.music_poll_interval_seconds,
                allowed_download_hosts=settings.music_allowed_download_hosts,
            )
        )
    if not settings.music_base_url or not settings.music_api_key:
        raise RuntimeError("外部配乐服务配置不完整")
    return track_provider(
        MiniMaxMusicGenerationPort(
            **common,
            base_url=settings.music_base_url,
            api_key=settings.music_api_key,
            timeout_seconds=settings.music_timeout_seconds,
            default_response_format=settings.music_response_format,
            allowed_download_hosts=settings.music_allowed_download_hosts,
        )
    )


@lru_cache(maxsize=1)
def get_subtitle_alignment_port() -> SubtitleAlignmentPort:
    settings = get_runtime_settings()
    return LocalSubtitleAlignmentPort(
        default_max_chars_per_line=settings.subtitle_max_chars_per_line,
        default_max_lines=settings.subtitle_max_lines,
    )


@configured_provider("video")
def get_ai_video_generation_port(settings: Settings) -> AiVideoGenerationPort:
    if settings.ai_video_provider == "mock":
        return MockAiVideoGenerationPort(
            provider_id=settings.ai_video_provider_id,
            model=settings.ai_video_model,
            supported_durations=settings.ai_video_supported_durations,
            supported_ratios=settings.ai_video_supported_ratios,
            supported_resolutions=settings.ai_video_supported_resolutions,
        )
    if settings.ai_video_provider == "hiagent":
        return track_provider(
            HiAgentVideoGenerationPort(
                client=hiagent_client(
                    settings, settings.ai_video_api_key, settings.ai_video_timeout_seconds
                ),
                pro_model=settings.ai_video_model,
                fast_model=settings.hiagent_video_fallback_model or settings.ai_video_model,
                provider_id=settings.ai_video_provider_id,
                durations=settings.ai_video_supported_durations,
                ratios=settings.ai_video_supported_ratios,
                resolutions=settings.ai_video_supported_resolutions,
            )
        )
    if not settings.ai_video_base_url or not settings.ai_video_api_key:
        raise RuntimeError("外部 AI 视频模型配置不完整")
    return track_provider(
        VolcengineArkAiVideoGenerationPort(
            provider_id=settings.ai_video_provider_id,
            base_url=settings.ai_video_base_url,
            api_key=settings.ai_video_api_key,
            model=settings.ai_video_model,
            timeout_seconds=settings.ai_video_timeout_seconds,
            supported_durations=settings.ai_video_supported_durations,
            supported_ratios=settings.ai_video_supported_ratios,
            supported_resolutions=settings.ai_video_supported_resolutions,
        )
    )


@lru_cache(maxsize=1)
def get_video_composer_port() -> VideoComposerPort:
    settings = get_runtime_settings()
    return FfmpegVideoComposerPort(
        ffmpeg_path=settings.ffmpeg_path,
        timeout_seconds=settings.ffmpeg_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_timeline_renderer() -> FfmpegTimelineRenderer:
    settings = get_runtime_settings()
    roots = (settings.data_dir,)
    return FfmpegTimelineRenderer(
        ffmpeg_path=settings.ffmpeg_path,
        timeout_seconds=settings.ffmpeg_timeout_seconds,
        allowed_input_roots=roots,
        allowed_output_roots=roots,
    )


@lru_cache(maxsize=1)
def get_provider_media_url_signer() -> ProviderMediaUrlSigner:
    settings = get_runtime_settings()
    return ProviderMediaUrlSigner(
        base_url=settings.provider_media_base_url,
        signing_secret=settings.provider_media_signing_secret,
        ttl_seconds=settings.provider_media_url_ttl_seconds,
    )


@lru_cache(maxsize=1)
def get_asset_store_port() -> AssetStorePort:
    settings = get_runtime_settings()
    local_store = LocalAssetStore(
        settings.data_dir / "objects",
        provider_media_url_signer=get_provider_media_url_signer(),
    )
    # Stopping new uploads must not break downloads of already hosted originals.
    if all((settings.weknora_base_url, settings.weknora_api_key, settings.weknora_knowledge_base_id)):
        from .db import SessionLocal
        from .original_archives import RemoteArchiveAssetStore

        return RemoteArchiveAssetStore(local_store, SessionLocal, get_weknora_client())
    return local_store


@lru_cache(maxsize=1)
def get_provider_media_publisher() -> ProviderMediaPublisherPort:
    settings = get_runtime_settings()
    if settings.provider_media_tos_endpoint and (
        settings.provider_media_relay_base_url or settings.provider_media_relay_token
    ):
        raise RuntimeError("TOS 与独立 HTTPS 素材中转只能配置一种，请明确选择发布通道")
    if not settings.provider_media_tos_endpoint:
        if settings.provider_media_relay_base_url or settings.provider_media_relay_token:
            if (
                not settings.provider_media_relay_base_url
                or not settings.provider_media_relay_token
            ):
                raise RuntimeError("HTTPS 素材中转配置不完整")
            return HttpMediaRelayPublisher(
                base_url=settings.provider_media_relay_base_url,
                token=settings.provider_media_relay_token,
                url_ttl_seconds=settings.provider_media_relay_url_ttl_seconds,
                allowed_root=settings.data_dir / "objects",
            )
        return DisabledProviderMediaPublisher()
    if not all(
        (
            settings.provider_media_tos_region,
            settings.provider_media_tos_bucket,
            settings.provider_media_tos_access_key,
            settings.provider_media_tos_secret_key,
        )
    ):
        # load_settings already validates this; retain a fail-closed boundary
        # for tests/integrations that construct Settings by replacement.
        raise RuntimeError("TOS 临时媒体发布配置不完整")
    return TosProviderMediaPublisher(
        endpoint=settings.provider_media_tos_endpoint,
        region=settings.provider_media_tos_region,
        bucket=settings.provider_media_tos_bucket,
        access_key=settings.provider_media_tos_access_key,
        secret_key=settings.provider_media_tos_secret_key,
        security_token=settings.provider_media_tos_security_token,
        key_prefix=settings.provider_media_tos_key_prefix,
        url_ttl_seconds=settings.provider_media_tos_url_ttl_seconds,
        object_ttl_days=settings.provider_media_tos_object_ttl_days,
        allowed_root=settings.data_dir / "objects",
        connect_timeout_seconds=settings.provider_media_tos_connect_timeout_seconds,
        socket_timeout_seconds=settings.provider_media_tos_socket_timeout_seconds,
    )


@lru_cache(maxsize=1)
def get_knowledge_catalog_port() -> KnowledgeCatalogPort:
    settings = get_runtime_settings()
    return LocalKnowledgeCatalog(
        settings.data_dir / "knowledge-catalog",
        settings.knowledge_namespace,
    )


# Short aliases ease route migration without leaking a supplier name into main.py.
get_image_provider = get_image_generation_port
get_speech_provider = get_speech_synthesis_port
get_music_provider = get_music_generation_port
get_ai_video_provider = get_ai_video_generation_port
