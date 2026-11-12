import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .http_media_relay import validate_relay_base_url, validate_relay_token

ARK_RUNTIME_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
VOLCENGINE_SPEECH_BASE_URL = "https://openspeech.bytedance.com"
DEFAULT_VOLCENGINE_SPEECH_RESOURCE_ID = "seed-tts-2.0"
DEFAULT_VOLCENGINE_SPEECH_VOICE = "zh_female_vv_uranus_bigtts"
DEFAULT_VOLCENGINE_SPEECH_VOICES = (
    DEFAULT_VOLCENGINE_SPEECH_VOICE,
    "zh_male_dayi_saturn_bigtts",
    "zh_female_santongyongns_saturn_bigtts",
)
DEFAULT_ARK_TEXT_MODEL = "chat-ds-flash"
DEFAULT_ARK_IMAGE_MODEL = "doubao-seedream-5-0-260128"
DEFAULT_ARK_VIDEO_MODEL = "doubao-seedance-2-0-fast-260128"
HIAGENT_IMAGE_MODEL = "d9k56d4mgqo5afvrboqg"
HIAGENT_VIDEO_FAST_MODEL = "d9k55hkmgqo5afvrboo0"
HIAGENT_VIDEO_PRO_MODEL = "d9k53bcmgqo5afvrbok0"
MIN_AI_VIDEO_GENERATION_SECONDS = 5
DEFAULT_SEEDANCE_2_FAST_DURATIONS = tuple(range(MIN_AI_VIDEO_GENERATION_SECONDS, 16))
DEFAULT_SEEDANCE_2_FAST_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive")
DEFAULT_SEEDANCE_2_FAST_RESOLUTIONS = ("480p", "720p")
MINIMAX_MUSIC_BASE_URL = "https://api.minimaxi.com"
DEFAULT_MINIMAX_MUSIC_MODEL = "music-2.6"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_url: str
    provider_media_base_url: str | None
    provider_media_signing_secret: str | None = field(repr=False)
    provider_media_url_ttl_seconds: int
    provider_media_tos_endpoint: str | None
    provider_media_tos_region: str | None
    provider_media_tos_bucket: str | None
    provider_media_tos_access_key: str | None = field(repr=False)
    provider_media_tos_secret_key: str | None = field(repr=False)
    provider_media_tos_security_token: str | None = field(repr=False)
    provider_media_tos_key_prefix: str
    provider_media_tos_url_ttl_seconds: int
    provider_media_tos_object_ttl_days: int
    provider_media_tos_connect_timeout_seconds: float
    provider_media_tos_socket_timeout_seconds: float
    llm_provider: str
    llm_provider_id: str
    llm_base_url: str | None
    llm_api_key: str | None = field(repr=False)
    llm_model: str
    llm_fallback_model: str | None
    llm_timeout_seconds: float
    llm_max_output_tokens: int
    llm_temperature: float
    llm_max_attempts: int
    knowledge_provider: str
    knowledge_top_k: int
    knowledge_namespace: str
    knowledge_dir: Path | None
    image_provider: str
    image_provider_id: str
    image_base_url: str | None
    image_api_key: str | None = field(repr=False)
    image_model: str
    image_timeout_seconds: float
    image_max_candidates: int
    image_supported_sizes: tuple[str, ...]
    image_supported_ratios: tuple[str, ...]
    speech_provider: str
    speech_provider_id: str
    speech_base_url: str | None
    speech_api_key: str | None = field(repr=False)
    speech_app_id: str | None
    speech_access_token: str | None = field(repr=False)
    speech_resource_id: str
    speech_cluster: str
    speech_voice_type: str
    speech_timeout_seconds: float
    speech_supported_voices: tuple[str, ...]
    music_provider: str
    music_provider_id: str
    music_base_url: str | None
    music_api_key: str | None = field(repr=False)
    music_access_key: str | None = field(repr=False)
    music_secret_key: str | None = field(repr=False)
    music_billing: str
    music_poll_interval_seconds: float
    music_model: str
    music_timeout_seconds: float
    music_max_bytes: int
    music_response_format: str
    music_min_duration_seconds: float
    music_max_duration_seconds: float
    music_allowed_download_hosts: tuple[str, ...]
    subtitle_max_chars_per_line: int
    subtitle_max_lines: int
    ai_video_provider: str
    ai_video_provider_id: str
    ai_video_base_url: str | None
    ai_video_api_key: str | None = field(repr=False)
    ai_video_model: str
    ai_video_timeout_seconds: float
    ai_video_poll_interval_seconds: float
    ai_video_max_wait_seconds: float
    ai_video_supported_durations: tuple[int, ...]
    ai_video_supported_ratios: tuple[str, ...]
    ai_video_supported_resolutions: tuple[str, ...]
    ffmpeg_path: str | None
    ffmpeg_timeout_seconds: float
    antivirus_command: str | None
    antivirus_timeout_seconds: float
    antivirus_required: bool
    project_media_quota_bytes: int
    provider_media_relay_base_url: str | None = None
    provider_media_relay_token: str | None = field(default=None, repr=False)
    provider_media_relay_url_ttl_seconds: int = 1800
    hiagent_secret_key: str | None = field(default=None, repr=False)
    hiagent_access_key: str | None = field(default=None, repr=False)
    hiagent_workspace_id: str = ""
    hiagent_base_url: str = ""
    hiagent_video_fallback_model: str = HIAGENT_VIDEO_FAST_MODEL
    weknora_base_url: str | None = None
    weknora_api_key: str | None = field(default=None, repr=False)
    weknora_knowledge_base_id: str | None = None
    weknora_tenant_id: str | None = None
    weknora_timeout_seconds: float = 30
    weknora_sync_enabled: bool = False
    weknora_originals_enabled: bool = False
    weknora_originals_max_file_bytes: int = 50 * 1024 * 1024
    weknora_sync_interval_seconds: float = 30
    weknora_allowed_data_levels: frozenset[str] = frozenset({"public", "internal"})
    # Tried in order after image_model fails (same base URL and key).
    image_fallback_models: tuple[str, ...] = ()
    # Chat model that drives the image tool when image_provider is openai_responses.
    image_responses_model: str = ""


def _optional_path(env_name: str) -> Path | None:
    value = os.getenv(env_name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def _optional_provider_media_base_url() -> str | None:
    value = os.getenv("CONTENT_PROVIDER_MEDIA_BASE_URL", "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "CONTENT_PROVIDER_MEDIA_BASE_URL 必须是无凭据、无查询串、无片段的 HTTPS URL"
        )
    return value.rstrip("/")


def _load_secret(env_name: str, file_env_name: str) -> str | None:
    direct = os.getenv(env_name, "").strip()
    if direct:
        return direct
    key_file = _optional_path(file_env_name)
    if key_file is None:
        return None
    if not key_file.is_file():
        raise ValueError(f"{file_env_name} 指向的文件不存在")
    value = key_file.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{file_env_name} 内容为空")
    return value


def _load_ark_api_key() -> str | None:
    return _load_secret("CONTENT_ARK_API_KEY", "CONTENT_ARK_API_KEY_FILE")


def _load_hiagent_access_key() -> str | None:
    return _load_secret("HIAGENT_AK", "HIAGENT_AK_FILE")


def _load_hiagent_secret_key() -> str | None:
    return _load_secret("HIAGENT_SK", "HIAGENT_SK_FILE")


def _hiagent_workspace_id() -> str:
    return os.getenv("HIAGENT_WORKSPACE_ID", "").strip()


def _load_api_key(fallback: str | None = None) -> str | None:
    return _load_secret("CONTENT_LLM_API_KEY", "CONTENT_LLM_API_KEY_FILE") or fallback


def _positive_float(env_name: str, default: str) -> float:
    value = float(os.getenv(env_name, default))
    if value <= 0:
        raise ValueError(f"{env_name} 必须大于 0")
    return value


def _boolean(env_name: str, default: str = "false") -> bool:
    value = os.getenv(env_name, default).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{env_name} 必须是布尔值")
    return value in {"true", "1", "yes"}


def _csv_values(env_name: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value.strip() for value in os.getenv(env_name, "").split(",") if value.strip()
        )
    )


def _csv_positive_ints(env_name: str) -> tuple[int, ...]:
    values = _csv_values(env_name)
    parsed = tuple(int(value) for value in values)
    if any(value <= 0 for value in parsed):
        raise ValueError(f"{env_name} 中的值必须全部大于 0")
    return parsed


def _ai_video_generation_durations(values: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(value for value in values if value >= MIN_AI_VIDEO_GENERATION_SECONDS)


def _ai_video_capability_defaults(
    provider: str,
    model: str,
) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...]]:
    """Return conservative model-specific defaults when no operator override exists."""

    if provider == "mock":
        return (5, 10), ("16:9", "9:16", "1:1"), ("480p", "720p")
    normalized_model = model.strip().lower()
    if "doubao-seedance-2-0-fast" in normalized_model:
        return (
            DEFAULT_SEEDANCE_2_FAST_DURATIONS,
            DEFAULT_SEEDANCE_2_FAST_RATIOS,
            DEFAULT_SEEDANCE_2_FAST_RESOLUTIONS,
        )
    if normalized_model in {HIAGENT_VIDEO_FAST_MODEL, HIAGENT_VIDEO_PRO_MODEL}:
        return (
            DEFAULT_SEEDANCE_2_FAST_DURATIONS,
            DEFAULT_SEEDANCE_2_FAST_RATIOS,
            ("480p", "720p")
            if normalized_model == HIAGENT_VIDEO_FAST_MODEL
            else ("480p", "720p", "1080p"),
        )
    return (), (), ()


def _provider_configuration(
    *,
    prefix: str,
    supported: set[str],
    default: str = "mock",
    fallback_api_key: str | None = None,
) -> tuple[str, str, str | None, str | None, str]:
    provider = os.getenv(f"{prefix}_PROVIDER", "").strip().lower() or default
    if provider not in supported:
        names = "、".join(sorted(supported))
        raise ValueError(f"{prefix}_PROVIDER 仅支持 {names}")
    provider_id = os.getenv(f"{prefix}_PROVIDER_ID", "").strip()
    base_url = os.getenv(f"{prefix}_BASE_URL", "").strip() or None
    api_key = _load_secret(f"{prefix}_API_KEY", f"{prefix}_API_KEY_FILE") or fallback_api_key
    model = os.getenv(f"{prefix}_MODEL", "").strip()
    return provider, provider_id, base_url, api_key, model


def load_settings() -> Settings:
    data_dir = Path(os.getenv("CONTENT_DATA_DIR", ".data/content")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    database_url = os.getenv("CONTENT_DATABASE_URL", f"sqlite:///{data_dir / 'content.db'}")
    ark_api_key = _load_ark_api_key()
    hiagent_access_key = _load_hiagent_access_key()
    hiagent_secret_key = _load_hiagent_secret_key()
    hiagent_workspace_id = _hiagent_workspace_id()
    hiagent_base_url = os.getenv("HIAGENT_BASE_URL", "").strip()
    ark_default_provider = "volcengine_ark" if ark_api_key else "mock"
    llm_provider = os.getenv("CONTENT_LLM_PROVIDER", "").strip().lower() or ark_default_provider
    if llm_provider not in {"mock", "openai_compatible", "volcengine_ark"}:
        raise ValueError("CONTENT_LLM_PROVIDER 仅支持 mock、openai_compatible 或 volcengine_ark")

    default_provider_id = {
        "mock": "mock-text",
        "volcengine_ark": "volcengine-ark-text",
    }.get(llm_provider, "")
    llm_provider_id = os.getenv("CONTENT_LLM_PROVIDER_ID", "").strip() or default_provider_id
    llm_base_url = os.getenv("CONTENT_LLM_BASE_URL", "").strip() or (
        ARK_RUNTIME_BASE_URL if llm_provider == "volcengine_ark" else None
    )
    llm_api_key = _load_api_key(ark_api_key)
    default_model = {
        "mock": "deterministic-text",
        "volcengine_ark": DEFAULT_ARK_TEXT_MODEL,
    }.get(llm_provider, "")
    llm_model = os.getenv("CONTENT_LLM_MODEL", "").strip() or default_model
    llm_fallback_model = os.getenv("CONTENT_LLM_FALLBACK_MODEL", "").strip() or None
    if llm_fallback_model == llm_model:
        raise ValueError("CONTENT_LLM_FALLBACK_MODEL 不能与 CONTENT_LLM_MODEL 相同")
    if llm_provider in {"openai_compatible", "volcengine_ark"}:
        missing = [
            name
            for name, value in (
                ("CONTENT_LLM_PROVIDER_ID", llm_provider_id),
                ("CONTENT_LLM_BASE_URL", llm_base_url),
                ("CONTENT_LLM_API_KEY/CONTENT_LLM_API_KEY_FILE", llm_api_key),
                ("CONTENT_LLM_MODEL", llm_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"外部内容模型缺少配置：{', '.join(missing)}")

    knowledge_provider = os.getenv("CONTENT_KNOWLEDGE_PROVIDER", "library_db").strip().lower()
    if knowledge_provider not in {"library_db", "jsonl", "weknora"}:
        raise ValueError("CONTENT_KNOWLEDGE_PROVIDER 仅支持 library_db、jsonl 或 weknora")
    knowledge_dir = _optional_path("CONTENT_KNOWLEDGE_DIR")
    if knowledge_provider == "jsonl" and knowledge_dir is None:
        raise ValueError("jsonl 知识库必须显式配置 CONTENT_KNOWLEDGE_DIR")
    weknora_base_url = os.getenv("CONTENT_WEKNORA_BASE_URL", "").strip().rstrip("/") or None
    weknora_api_key = _load_secret("CONTENT_WEKNORA_API_KEY", "CONTENT_WEKNORA_API_KEY_FILE")
    weknora_knowledge_base_id = os.getenv("CONTENT_WEKNORA_KNOWLEDGE_BASE_ID", "").strip() or None
    weknora_tenant_id = os.getenv("CONTENT_WEKNORA_TENANT_ID", "").strip() or None
    weknora_sync_enabled = _boolean("CONTENT_WEKNORA_SYNC_ENABLED")
    weknora_originals_enabled = _boolean("CONTENT_WEKNORA_ORIGINALS_ENABLED")
    weknora_originals_max_file_bytes = int(os.getenv("CONTENT_WEKNORA_ORIGINALS_MAX_FILE_BYTES", "52428800"))
    if not 1 <= weknora_originals_max_file_bytes <= 1024 * 1024 * 1024:
        raise ValueError("原件托管大小上限必须在 1 字节到 1 GiB 之间")
    weknora_timeout_seconds = _positive_float("CONTENT_WEKNORA_TIMEOUT_SECONDS", "30")
    weknora_sync_interval_seconds = _positive_float("CONTENT_WEKNORA_SYNC_INTERVAL_SECONDS", "30")
    weknora_allowed_data_levels = frozenset(
        part.strip() for part in os.getenv(
            "CONTENT_WEKNORA_ALLOWED_DATA_LEVELS", "public,internal"
        ).split(",") if part.strip()
    )
    if not weknora_allowed_data_levels or not weknora_allowed_data_levels <= {
        "public", "internal", "restricted"
    }:
        raise ValueError("CONTENT_WEKNORA_ALLOWED_DATA_LEVELS 必须是 public、internal、restricted 的子集")
    if weknora_sync_enabled and knowledge_provider != "weknora":
        raise ValueError("知识同步需要 CONTENT_KNOWLEDGE_PROVIDER=weknora")
    if (knowledge_provider == "weknora" or weknora_originals_enabled
            or any((weknora_base_url, weknora_api_key, weknora_knowledge_base_id))):
        if not all((weknora_base_url, weknora_api_key, weknora_knowledge_base_id)):
            raise ValueError("WeKnora 需要配置接口地址、专用 API Key 和知识库 ID")
        parsed = urlsplit(weknora_base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") not in {"", "/api/v1"}
                or re.search(r"[\x00-\x20\x7f\\?#]", weknora_base_url)):
            raise ValueError("WeKnora 接口地址须为无凭据的 HTTP(S) 服务地址或 /api/v1 地址")
        try:
            if parsed.port == 0:
                raise ValueError
        except ValueError:
            raise ValueError("WeKnora 接口端口无效") from None
        if (not weknora_api_key.isascii()
                or re.search(r"[\x00-\x20\x7f]", weknora_api_key)):
            raise ValueError("WeKnora API Key 不允许空白或控制字符")
        if (weknora_knowledge_base_id in {".", ".."} or len(weknora_knowledge_base_id) > 255
                or re.search(r"[\x00-\x20\x7f/\\?#]", weknora_knowledge_base_id)):
            raise ValueError("WeKnora 知识库 ID 无效")
        if weknora_tenant_id and not re.fullmatch(r"[1-9][0-9]*", weknora_tenant_id):
            raise ValueError("WeKnora 平台空间 ID 必须是正整数")
        if not 1 <= weknora_timeout_seconds <= 300:
            raise ValueError("WeKnora 请求超时必须在 1 到 300 秒之间")
        if not 1 <= weknora_sync_interval_seconds <= 3600:
            raise ValueError("WeKnora 同步间隔必须在 1 到 3600 秒之间")

    knowledge_top_k = int(os.getenv("CONTENT_KNOWLEDGE_TOP_K", "4"))
    if knowledge_top_k < 1:
        raise ValueError("CONTENT_KNOWLEDGE_TOP_K 必须大于等于 1")
    llm_max_attempts = int(os.getenv("CONTENT_LLM_MAX_ATTEMPTS", "2"))
    if llm_max_attempts < 1:
        raise ValueError("CONTENT_LLM_MAX_ATTEMPTS 必须大于等于 1")
    llm_timeout_seconds = float(os.getenv("CONTENT_LLM_TIMEOUT_SECONDS", "120"))
    if llm_timeout_seconds <= 0:
        raise ValueError("CONTENT_LLM_TIMEOUT_SECONDS 必须大于 0")
    llm_max_output_tokens = int(os.getenv("CONTENT_LLM_MAX_OUTPUT_TOKENS", "4000"))
    if llm_max_output_tokens < 1:
        raise ValueError("CONTENT_LLM_MAX_OUTPUT_TOKENS 必须大于等于 1")
    llm_temperature = float(os.getenv("CONTENT_LLM_TEMPERATURE", "0.2"))
    if not 0 <= llm_temperature <= 2:
        raise ValueError("CONTENT_LLM_TEMPERATURE 必须在 0 到 2 之间")

    image_provider_name = (
        os.getenv("CONTENT_IMAGE_PROVIDER", "").strip().lower() or ark_default_provider
    )
    image_fallback_key = (
        hiagent_access_key
        if image_provider_name == "hiagent"
        else (None if image_provider_name == "openai_compatible" else (ark_api_key or hiagent_access_key))
    )
    image_provider, image_provider_id, image_base_url, image_api_key, image_model = (
        _provider_configuration(
            prefix="CONTENT_IMAGE",
            supported={"mock", "volcengine_ark", "hiagent", "openai_compatible"},
            default=ark_default_provider,
            fallback_api_key=image_fallback_key,
        )
    )
    if image_provider == "mock":
        image_provider_id = image_provider_id or "mock-image"
        image_model = image_model or "deterministic-image"
    elif image_provider in {"volcengine_ark", "openai_compatible"}:
        image_provider_id = image_provider_id or image_provider.replace("_", "-") + "-image"
        if image_provider == "volcengine_ark":
            image_base_url = image_base_url or ARK_RUNTIME_BASE_URL
            image_model = image_model or DEFAULT_ARK_IMAGE_MODEL
        missing = [
            name
            for name, value in (
                ("CONTENT_IMAGE_PROVIDER_ID", image_provider_id),
                ("CONTENT_IMAGE_BASE_URL", image_base_url),
                ("CONTENT_IMAGE_API_KEY/CONTENT_IMAGE_API_KEY_FILE", image_api_key),
                ("CONTENT_IMAGE_MODEL", image_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"外部图片模型缺少配置：{', '.join(missing)}")
    else:
        image_provider_id = image_provider_id or "hiagent-seedream-image"
        image_model = image_model or HIAGENT_IMAGE_MODEL
        missing = [
            name
            for name, value in (
                ("HIAGENT_AK/CONTENT_IMAGE_API_KEY", image_api_key),
                ("HIAGENT_SK/HIAGENT_SK_FILE", hiagent_secret_key),
                ("HIAGENT_WORKSPACE_ID", hiagent_workspace_id),
                ("HIAGENT_BASE_URL", hiagent_base_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"HiAgent 图片模型缺少配置：{', '.join(missing)}")

    speech_api_key = _load_secret(
        "CONTENT_SPEECH_API_KEY",
        "CONTENT_SPEECH_API_KEY_FILE",
    )
    speech_default_provider = "volcengine_tts_api_key" if speech_api_key else "mock"
    speech_provider = (
        os.getenv("CONTENT_SPEECH_PROVIDER", "").strip().lower() or speech_default_provider
    )
    if speech_provider not in {"mock", "volcengine_tts_api_key", "volcengine_tts", "chat-tts"}:
        raise ValueError(
            "CONTENT_SPEECH_PROVIDER 仅支持 mock、volcengine_tts_api_key、volcengine_tts 或 chat-tts"
        )
    speech_provider_id = os.getenv("CONTENT_SPEECH_PROVIDER_ID", "").strip()
    speech_base_url = os.getenv("CONTENT_SPEECH_BASE_URL", "").strip() or None
    speech_app_id = os.getenv("CONTENT_SPEECH_APP_ID", "").strip() or None
    speech_access_token = _load_secret(
        "CONTENT_SPEECH_ACCESS_TOKEN",
        "CONTENT_SPEECH_ACCESS_TOKEN_FILE",
    )
    speech_resource_id = os.getenv("CONTENT_SPEECH_RESOURCE_ID", "").strip()
    speech_voice_type = os.getenv("CONTENT_SPEECH_VOICE_TYPE", "").strip()
    if speech_provider == "mock":
        speech_provider_id = speech_provider_id or "mock-speech"
        speech_resource_id = speech_resource_id or "mock-resource"
        speech_voice_type = speech_voice_type or "mock-voice"
    elif speech_provider == "chat-tts":
        speech_provider_id = speech_provider_id or "chat-tts"
        if not speech_base_url:
            raise ValueError("chat-tts 语音缺少配置：CONTENT_SPEECH_BASE_URL")
        speech_api_key = speech_api_key or llm_api_key
        speech_resource_id = speech_resource_id or "chat-tts"
        speech_voice_type = speech_voice_type or "vivian"
    elif speech_provider == "volcengine_tts_api_key":
        speech_provider_id = speech_provider_id or "volcengine-doubao-speech-tts"
        speech_base_url = speech_base_url or VOLCENGINE_SPEECH_BASE_URL
        speech_resource_id = speech_resource_id or DEFAULT_VOLCENGINE_SPEECH_RESOURCE_ID
        speech_voice_type = speech_voice_type or DEFAULT_VOLCENGINE_SPEECH_VOICE
        missing = [
            name
            for name, value in (
                (
                    "CONTENT_SPEECH_API_KEY/CONTENT_SPEECH_API_KEY_FILE",
                    speech_api_key,
                ),
                ("CONTENT_SPEECH_BASE_URL", speech_base_url),
                ("CONTENT_SPEECH_RESOURCE_ID", speech_resource_id),
                ("CONTENT_SPEECH_VOICE_TYPE", speech_voice_type),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"新版外部配音服务缺少配置：{', '.join(missing)}")
    else:
        missing = [
            name
            for name, value in (
                ("CONTENT_SPEECH_PROVIDER_ID", speech_provider_id),
                ("CONTENT_SPEECH_BASE_URL", speech_base_url),
                ("CONTENT_SPEECH_APP_ID", speech_app_id),
                (
                    "CONTENT_SPEECH_ACCESS_TOKEN/CONTENT_SPEECH_ACCESS_TOKEN_FILE",
                    speech_access_token,
                ),
                ("CONTENT_SPEECH_RESOURCE_ID", speech_resource_id),
                ("CONTENT_SPEECH_VOICE_TYPE", speech_voice_type),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"外部配音服务缺少配置：{', '.join(missing)}")

    music_api_key = _load_secret("CONTENT_MUSIC_API_KEY", "CONTENT_MUSIC_API_KEY_FILE")
    music_access_key = _load_secret("CONTENT_MUSIC_ACCESS_KEY", "CONTENT_MUSIC_ACCESS_KEY_FILE")
    music_secret_key = _load_secret("CONTENT_MUSIC_SECRET_KEY", "CONTENT_MUSIC_SECRET_KEY_FILE")
    # Mock output is only available through explicit development/test configuration.
    # Ark's bearer key is not an Imagination AK/SK credential.
    music_default_provider = "minimax" if music_api_key else "volcengine"
    music_provider = (
        os.getenv("CONTENT_MUSIC_PROVIDER", "").strip().lower() or music_default_provider
    )
    if music_provider not in {"mock", "minimax", "volcengine", "seedance", "hiagent_seedance"}:
        raise ValueError(
            "CONTENT_MUSIC_PROVIDER 仅支持 mock、minimax、volcengine、seedance 或 hiagent_seedance"
        )
    music_billing = os.getenv("CONTENT_MUSIC_BILLING", "prepaid").strip().lower()
    if music_billing not in {"prepaid", "postpaid"}:
        raise ValueError("CONTENT_MUSIC_BILLING 仅支持 prepaid 或 postpaid")
    music_provider_id = os.getenv("CONTENT_MUSIC_PROVIDER_ID", "").strip()
    music_base_url = os.getenv("CONTENT_MUSIC_BASE_URL", "").strip() or None
    music_model = os.getenv("CONTENT_MUSIC_MODEL", "").strip()
    if music_provider == "mock":
        music_provider_id = music_provider_id or "mock-music"
        music_model = music_model or "deterministic-instrumental"
    elif music_provider in {"seedance", "hiagent_seedance"}:
        music_provider_id = music_provider_id or "volcengine-ark-seedance-music"
        music_base_url = (
            music_base_url
            or os.getenv("CONTENT_AI_VIDEO_BASE_URL", "").strip()
            or ARK_RUNTIME_BASE_URL
        )
        # Settings are loaded while database models initialize; do not import adapters here.
        music_model = music_model or (
            HIAGENT_VIDEO_PRO_MODEL
            if music_provider == "hiagent_seedance"
            else "doubao-seedance-2-5-260628"
        )
        music_api_key = (
            music_api_key
            or _load_secret("CONTENT_AI_VIDEO_API_KEY", "CONTENT_AI_VIDEO_API_KEY_FILE")
            or ark_api_key
        )
    elif music_provider == "volcengine":
        music_provider_id = music_provider_id or "volcengine-bgm"
        music_base_url = music_base_url or "https://open.volcengineapi.com"
        music_model = music_model or "v5.0"
        # Missing credentials fail at the music action, never at application startup.
    else:
        music_provider_id = music_provider_id or "minimax-music"
        music_base_url = music_base_url or MINIMAX_MUSIC_BASE_URL
        music_model = music_model or DEFAULT_MINIMAX_MUSIC_MODEL
        missing = [
            name
            for name, value in (
                (
                    "CONTENT_MUSIC_API_KEY/CONTENT_MUSIC_API_KEY_FILE",
                    music_api_key,
                ),
                ("CONTENT_MUSIC_BASE_URL", music_base_url),
                ("CONTENT_MUSIC_MODEL", music_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"外部配乐服务缺少配置：{', '.join(missing)}")

    music_response_format = os.getenv("CONTENT_MUSIC_RESPONSE_FORMAT", "hex").strip().lower()
    if music_response_format not in {"hex", "url"}:
        raise ValueError("CONTENT_MUSIC_RESPONSE_FORMAT 仅支持 hex 或 url")
    music_max_bytes = int(os.getenv("CONTENT_MUSIC_MAX_BYTES", str(64 * 1024 * 1024)))
    if music_max_bytes < 1024:
        raise ValueError("CONTENT_MUSIC_MAX_BYTES 必须大于等于 1024")
    music_min_duration_seconds = _positive_float(
        "CONTENT_MUSIC_MIN_DURATION_SECONDS",
        "30" if music_provider == "volcengine" else "5" if music_provider == "seedance" else "1",
    )
    seedance_music_max_seconds = 15 if "seedance-2-0-" in music_model else 30
    music_max_duration_seconds = _positive_float(
        "CONTENT_MUSIC_MAX_DURATION_SECONDS",
        "120"
        if music_provider == "volcengine"
        else str(seedance_music_max_seconds)
        if music_provider == "seedance"
        else "300",
    )
    if music_provider == "volcengine":
        music_min_duration_seconds = max(30, music_min_duration_seconds)
        music_max_duration_seconds = min(120, music_max_duration_seconds)
    elif music_provider in {"seedance", "hiagent_seedance"}:
        if (
            not 5
            <= music_min_duration_seconds
            <= music_max_duration_seconds
            <= seedance_music_max_seconds
        ):
            raise ValueError(
                f"当前 Seedance 背景音乐模型的上游生成时长必须在 5 到 {seedance_music_max_seconds} 秒内"
            )
    if music_max_duration_seconds < music_min_duration_seconds:
        raise ValueError("配乐最大时长不能小于最小时长")

    (
        ai_video_provider,
        ai_video_provider_id,
        ai_video_base_url,
        ai_video_api_key,
        ai_video_model,
    ) = _provider_configuration(
        prefix="CONTENT_AI_VIDEO",
        supported={"mock", "volcengine_ark", "hiagent"},
        default=ark_default_provider,
        fallback_api_key=(
            hiagent_access_key
            if (os.getenv("CONTENT_AI_VIDEO_PROVIDER", "").strip().lower() or ark_default_provider)
            == "hiagent"
            else (ark_api_key or hiagent_access_key)
        ),
    )
    if ai_video_provider == "mock":
        ai_video_provider_id = ai_video_provider_id or "mock-ai-video"
        ai_video_model = ai_video_model or "deterministic-video-task"
    elif ai_video_provider == "volcengine_ark":
        ai_video_provider_id = ai_video_provider_id or "volcengine-ark-ai-video"
        ai_video_base_url = ai_video_base_url or ARK_RUNTIME_BASE_URL
        ai_video_model = ai_video_model or DEFAULT_ARK_VIDEO_MODEL
        missing = [
            name
            for name, value in (
                ("CONTENT_AI_VIDEO_PROVIDER_ID", ai_video_provider_id),
                ("CONTENT_AI_VIDEO_BASE_URL", ai_video_base_url),
                (
                    "CONTENT_AI_VIDEO_API_KEY/CONTENT_AI_VIDEO_API_KEY_FILE",
                    ai_video_api_key,
                ),
                ("CONTENT_AI_VIDEO_MODEL", ai_video_model),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"外部 AI 视频模型缺少配置：{', '.join(missing)}")
    else:
        ai_video_provider_id = ai_video_provider_id or "hiagent-seedance-video"
        ai_video_model = ai_video_model or HIAGENT_VIDEO_PRO_MODEL
        missing = [
            name
            for name, value in (
                ("HIAGENT_AK/CONTENT_AI_VIDEO_API_KEY", ai_video_api_key),
                ("HIAGENT_SK/HIAGENT_SK_FILE", hiagent_secret_key),
                ("HIAGENT_WORKSPACE_ID", hiagent_workspace_id),
                ("HIAGENT_BASE_URL", hiagent_base_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"HiAgent 视频模型缺少配置：{', '.join(missing)}")

    ai_video_poll_interval_seconds = _positive_float(
        "CONTENT_AI_VIDEO_POLL_INTERVAL_SECONDS",
        "3",
    )
    ai_video_max_wait_seconds = _positive_float(
        "CONTENT_AI_VIDEO_MAX_WAIT_SECONDS",
        "900",
    )
    if ai_video_poll_interval_seconds > ai_video_max_wait_seconds:
        raise ValueError("AI 视频轮询间隔不能大于最大等待时间")
    (
        default_ai_video_durations,
        default_ai_video_ratios,
        default_ai_video_resolutions,
    ) = _ai_video_capability_defaults(ai_video_provider, ai_video_model)
    ai_video_supported_durations = _ai_video_generation_durations(
        _csv_positive_ints("CONTENT_AI_VIDEO_SUPPORTED_DURATIONS") or default_ai_video_durations
    )
    if not ai_video_supported_durations:
        raise ValueError("AI 视频时长必须包含至少一项 5 秒及以上的选项")
    ai_video_supported_ratios = (
        _csv_values("CONTENT_AI_VIDEO_SUPPORTED_RATIOS") or default_ai_video_ratios
    )
    ai_video_supported_resolutions = (
        _csv_values("CONTENT_AI_VIDEO_SUPPORTED_RESOLUTIONS") or default_ai_video_resolutions
    )
    subtitle_max_chars_per_line = int(os.getenv("CONTENT_SUBTITLE_MAX_CHARS_PER_LINE", "24"))
    subtitle_max_lines = int(os.getenv("CONTENT_SUBTITLE_MAX_LINES", "2"))
    if subtitle_max_chars_per_line < 1 or subtitle_max_lines < 1:
        raise ValueError("字幕每行字符数和最大行数必须大于 0")
    image_max_candidates = int(os.getenv("CONTENT_IMAGE_MAX_CANDIDATES", "4"))
    if image_max_candidates < 1:
        raise ValueError("CONTENT_IMAGE_MAX_CANDIDATES 必须大于 0")
    if image_provider == "hiagent" and not os.getenv("CONTENT_IMAGE_MAX_CANDIDATES", "").strip():
        image_max_candidates = 15
    project_media_quota_bytes = int(
        os.getenv("CONTENT_PROJECT_MEDIA_QUOTA_BYTES", str(20 * 1024 * 1024 * 1024))
    )
    if project_media_quota_bytes < 1:
        raise ValueError("CONTENT_PROJECT_MEDIA_QUOTA_BYTES 必须大于 0")

    provider_media_base_url = _optional_provider_media_base_url()
    if (
        os.getenv("CONTENT_PROVIDER_MEDIA_SIGNING_SECRET", "").strip()
        and os.getenv("CONTENT_PROVIDER_MEDIA_SIGNING_SECRET_FILE", "").strip()
    ):
        raise ValueError("Provider 媒体网关签名密钥只能配置直接值或文件中的一种")
    provider_media_signing_secret = _load_secret(
        "CONTENT_PROVIDER_MEDIA_SIGNING_SECRET",
        "CONTENT_PROVIDER_MEDIA_SIGNING_SECRET_FILE",
    )
    if bool(provider_media_base_url) != bool(provider_media_signing_secret):
        raise ValueError("Provider 媒体网关必须同时配置 CONTENT_PROVIDER_MEDIA_BASE_URL 和签名密钥")
    if provider_media_signing_secret and len(provider_media_signing_secret.encode("utf-8")) < 32:
        raise ValueError("CONTENT_PROVIDER_MEDIA_SIGNING_SECRET 至少需要 32 字节")
    provider_media_url_ttl_seconds = int(os.getenv("CONTENT_PROVIDER_MEDIA_URL_TTL_SECONDS", "900"))
    if not 60 <= provider_media_url_ttl_seconds <= 3600:
        raise ValueError("CONTENT_PROVIDER_MEDIA_URL_TTL_SECONDS 必须在 60 到 3600 之间")

    for direct_name, file_name in (
        (
            "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY",
            "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY_FILE",
        ),
        (
            "CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY",
            "CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY_FILE",
        ),
        (
            "CONTENT_PROVIDER_MEDIA_TOS_SECURITY_TOKEN",
            "CONTENT_PROVIDER_MEDIA_TOS_SECURITY_TOKEN_FILE",
        ),
    ):
        if os.getenv(direct_name, "").strip() and os.getenv(file_name, "").strip():
            raise ValueError(f"{direct_name} 与 {file_name} 只能配置一种")
    provider_media_tos_endpoint = (
        os.getenv("CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT", "").strip().rstrip("/") or None
    )
    provider_media_tos_region = os.getenv("CONTENT_PROVIDER_MEDIA_TOS_REGION", "").strip() or None
    provider_media_tos_bucket = os.getenv("CONTENT_PROVIDER_MEDIA_TOS_BUCKET", "").strip() or None
    provider_media_tos_access_key = _load_secret(
        "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY",
        "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY_FILE",
    )
    provider_media_tos_secret_key = _load_secret(
        "CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY",
        "CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY_FILE",
    )
    provider_media_tos_security_token = _load_secret(
        "CONTENT_PROVIDER_MEDIA_TOS_SECURITY_TOKEN",
        "CONTENT_PROVIDER_MEDIA_TOS_SECURITY_TOKEN_FILE",
    )
    tos_required = {
        "CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT": provider_media_tos_endpoint,
        "CONTENT_PROVIDER_MEDIA_TOS_REGION": provider_media_tos_region,
        "CONTENT_PROVIDER_MEDIA_TOS_BUCKET": provider_media_tos_bucket,
        (
            "CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY/CONTENT_PROVIDER_MEDIA_TOS_ACCESS_KEY_FILE"
        ): provider_media_tos_access_key,
        (
            "CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY/CONTENT_PROVIDER_MEDIA_TOS_SECRET_KEY_FILE"
        ): provider_media_tos_secret_key,
    }
    if any((*tos_required.values(), provider_media_tos_security_token)) and not all(
        tos_required.values()
    ):
        missing = [name for name, value in tos_required.items() if not value]
        raise ValueError(f"TOS 临时媒体发布配置不完整：{', '.join(missing)}")
    if provider_media_tos_endpoint:
        parsed_tos_endpoint = urlsplit(provider_media_tos_endpoint)
        if (
            parsed_tos_endpoint.scheme.lower() != "https"
            or not parsed_tos_endpoint.hostname
            or parsed_tos_endpoint.username is not None
            or parsed_tos_endpoint.password is not None
            or parsed_tos_endpoint.path not in {"", "/"}
            or parsed_tos_endpoint.query
            or parsed_tos_endpoint.fragment
        ):
            raise ValueError(
                "CONTENT_PROVIDER_MEDIA_TOS_ENDPOINT 必须是无路径、无凭据的 HTTPS 地址"
            )
    if provider_media_tos_bucket and not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
        provider_media_tos_bucket,
    ):
        raise ValueError("CONTENT_PROVIDER_MEDIA_TOS_BUCKET 格式无效")
    provider_media_tos_key_prefix = (
        os.getenv(
            "CONTENT_PROVIDER_MEDIA_TOS_KEY_PREFIX",
            "provider-media/reference-videos",
        )
        .strip()
        .strip("/")
    )
    if (
        not provider_media_tos_key_prefix
        or "\\" in provider_media_tos_key_prefix
        or any(part in {"", ".", ".."} for part in provider_media_tos_key_prefix.split("/"))
    ):
        raise ValueError("CONTENT_PROVIDER_MEDIA_TOS_KEY_PREFIX 必须是安全的 POSIX 相对路径")
    provider_media_tos_url_ttl_seconds = int(
        os.getenv("CONTENT_PROVIDER_MEDIA_TOS_URL_TTL_SECONDS", "900")
    )
    if not 60 <= provider_media_tos_url_ttl_seconds <= 3600:
        raise ValueError("CONTENT_PROVIDER_MEDIA_TOS_URL_TTL_SECONDS 必须在 60 到 3600 之间")
    provider_media_tos_object_ttl_days = int(
        os.getenv("CONTENT_PROVIDER_MEDIA_TOS_OBJECT_TTL_DAYS", "1")
    )
    if not 1 <= provider_media_tos_object_ttl_days <= 7:
        raise ValueError("CONTENT_PROVIDER_MEDIA_TOS_OBJECT_TTL_DAYS 必须在 1 到 7 之间")
    provider_media_tos_connect_timeout_seconds = _positive_float(
        "CONTENT_PROVIDER_MEDIA_TOS_CONNECT_TIMEOUT_SECONDS",
        "10",
    )
    provider_media_tos_socket_timeout_seconds = _positive_float(
        "CONTENT_PROVIDER_MEDIA_TOS_SOCKET_TIMEOUT_SECONDS",
        "60",
    )

    provider_media_relay_base_url = (
        os.getenv("CONTENT_PROVIDER_MEDIA_RELAY_BASE_URL", "").strip() or None
    )
    if (
        os.getenv("CONTENT_PROVIDER_MEDIA_RELAY_TOKEN", "").strip()
        and os.getenv("CONTENT_PROVIDER_MEDIA_RELAY_TOKEN_FILE", "").strip()
    ):
        raise ValueError(
            "CONTENT_PROVIDER_MEDIA_RELAY_TOKEN 与 CONTENT_PROVIDER_MEDIA_RELAY_TOKEN_FILE "
            "只能配置一种"
        )
    provider_media_relay_token = _load_secret(
        "CONTENT_PROVIDER_MEDIA_RELAY_TOKEN", "CONTENT_PROVIDER_MEDIA_RELAY_TOKEN_FILE"
    )
    if bool(provider_media_relay_base_url) != bool(provider_media_relay_token):
        raise ValueError("HTTPS 素材中转配置不完整：必须同时配置 BASE_URL 和 TOKEN/TOKEN_FILE")
    if provider_media_relay_base_url:
        provider_media_relay_base_url = validate_relay_base_url(provider_media_relay_base_url)
    if provider_media_relay_token:
        provider_media_relay_token = validate_relay_token(provider_media_relay_token)
    provider_media_relay_url_ttl_seconds = int(
        os.getenv("CONTENT_PROVIDER_MEDIA_RELAY_URL_TTL_SECONDS", "1800")
    )
    if not 60 <= provider_media_relay_url_ttl_seconds <= 3600:
        raise ValueError("CONTENT_PROVIDER_MEDIA_RELAY_URL_TTL_SECONDS 必须在 60 到 3600 之间")
    if provider_media_relay_base_url and provider_media_tos_endpoint:
        raise ValueError("TOS 与独立 HTTPS 素材中转只能配置一种，请明确选择发布通道")

    return Settings(
        data_dir=data_dir,
        database_url=database_url,
        provider_media_base_url=provider_media_base_url,
        provider_media_signing_secret=provider_media_signing_secret,
        provider_media_url_ttl_seconds=provider_media_url_ttl_seconds,
        provider_media_tos_endpoint=provider_media_tos_endpoint,
        provider_media_tos_region=provider_media_tos_region,
        provider_media_tos_bucket=provider_media_tos_bucket,
        provider_media_tos_access_key=provider_media_tos_access_key,
        provider_media_tos_secret_key=provider_media_tos_secret_key,
        provider_media_tos_security_token=provider_media_tos_security_token,
        provider_media_tos_key_prefix=provider_media_tos_key_prefix,
        provider_media_tos_url_ttl_seconds=provider_media_tos_url_ttl_seconds,
        provider_media_tos_object_ttl_days=provider_media_tos_object_ttl_days,
        provider_media_tos_connect_timeout_seconds=(provider_media_tos_connect_timeout_seconds),
        provider_media_tos_socket_timeout_seconds=(provider_media_tos_socket_timeout_seconds),
        provider_media_relay_base_url=provider_media_relay_base_url,
        provider_media_relay_token=provider_media_relay_token,
        provider_media_relay_url_ttl_seconds=provider_media_relay_url_ttl_seconds,
        llm_provider=llm_provider,
        hiagent_secret_key=hiagent_secret_key,
        hiagent_access_key=hiagent_access_key,
        hiagent_workspace_id=hiagent_workspace_id,
        hiagent_base_url=hiagent_base_url,
        hiagent_video_fallback_model=os.getenv(
            "HIAGENT_VIDEO_FAST_MODEL", HIAGENT_VIDEO_FAST_MODEL
        ).strip(),
        llm_provider_id=llm_provider_id,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_fallback_model=llm_fallback_model,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_max_output_tokens=llm_max_output_tokens,
        llm_temperature=llm_temperature,
        llm_max_attempts=llm_max_attempts,
        knowledge_provider=knowledge_provider,
        knowledge_top_k=knowledge_top_k,
        knowledge_namespace=os.getenv("CONTENT_KNOWLEDGE_NAMESPACE", "content-service").strip(),
        knowledge_dir=knowledge_dir,
        weknora_base_url=weknora_base_url,
        weknora_api_key=weknora_api_key,
        weknora_knowledge_base_id=weknora_knowledge_base_id,
        weknora_tenant_id=weknora_tenant_id,
        weknora_timeout_seconds=weknora_timeout_seconds,
        weknora_sync_enabled=weknora_sync_enabled,
        weknora_originals_enabled=weknora_originals_enabled,
        weknora_originals_max_file_bytes=weknora_originals_max_file_bytes,
        weknora_sync_interval_seconds=weknora_sync_interval_seconds,
        weknora_allowed_data_levels=weknora_allowed_data_levels,
        image_provider=image_provider,
        image_provider_id=image_provider_id,
        image_base_url=image_base_url,
        image_api_key=image_api_key,
        image_model=image_model,
        image_timeout_seconds=_positive_float("CONTENT_IMAGE_TIMEOUT_SECONDS", "180"),
        image_max_candidates=image_max_candidates,
        image_supported_sizes=_csv_values("CONTENT_IMAGE_SUPPORTED_SIZES"),
        image_supported_ratios=_csv_values("CONTENT_IMAGE_SUPPORTED_RATIOS"),
        image_fallback_models=_csv_values("CONTENT_IMAGE_FALLBACK_MODELS"),
        image_responses_model=os.getenv("CONTENT_IMAGE_RESPONSES_MODEL", "").strip(),
        speech_provider=speech_provider,
        speech_provider_id=speech_provider_id,
        speech_base_url=speech_base_url,
        speech_api_key=speech_api_key,
        speech_app_id=speech_app_id,
        speech_access_token=speech_access_token,
        speech_resource_id=speech_resource_id,
        speech_cluster=os.getenv("CONTENT_SPEECH_CLUSTER", "volcano_tts").strip() or "volcano_tts",
        speech_voice_type=speech_voice_type,
        speech_timeout_seconds=_positive_float("CONTENT_SPEECH_TIMEOUT_SECONDS", "180"),
        speech_supported_voices=(
            (speech_voice_type,)
            if speech_provider == "mock"
            else (
                tuple(
                    dict.fromkeys(
                        (
                            *_csv_values("CONTENT_SPEECH_SUPPORTED_VOICES"),
                            speech_voice_type,
                            *DEFAULT_VOLCENGINE_SPEECH_VOICES,
                        )
                    )
                )
                if speech_provider == "volcengine_tts_api_key"
                else _csv_values("CONTENT_SPEECH_SUPPORTED_VOICES")
                or (
                    (
                        "aiden",
                        "dylan",
                        "eric",
                        "ono_anna",
                        "ryan",
                        "serena",
                        "sohee",
                        "uncle_fu",
                        "vivian",
                    )
                    if speech_provider == "chat-tts"
                    else (speech_voice_type,)
                )
            )
        ),
        music_provider=music_provider,
        music_provider_id=music_provider_id,
        music_base_url=music_base_url,
        music_api_key=music_api_key,
        music_access_key=music_access_key,
        music_secret_key=music_secret_key,
        music_billing=music_billing,
        music_poll_interval_seconds=_positive_float("CONTENT_MUSIC_POLL_INTERVAL_SECONDS", "10"),
        music_model=music_model,
        music_timeout_seconds=_positive_float("CONTENT_MUSIC_TIMEOUT_SECONDS", "300"),
        music_max_bytes=music_max_bytes,
        music_response_format=music_response_format,
        music_min_duration_seconds=music_min_duration_seconds,
        music_max_duration_seconds=music_max_duration_seconds,
        music_allowed_download_hosts=tuple(
            value.lower().rstrip(".")
            for value in _csv_values("CONTENT_MUSIC_ALLOWED_DOWNLOAD_HOSTS")
        ),
        subtitle_max_chars_per_line=subtitle_max_chars_per_line,
        subtitle_max_lines=subtitle_max_lines,
        ai_video_provider=ai_video_provider,
        ai_video_provider_id=ai_video_provider_id,
        ai_video_base_url=ai_video_base_url,
        ai_video_api_key=ai_video_api_key,
        ai_video_model=ai_video_model,
        ai_video_timeout_seconds=_positive_float("CONTENT_AI_VIDEO_TIMEOUT_SECONDS", "60"),
        ai_video_poll_interval_seconds=ai_video_poll_interval_seconds,
        ai_video_max_wait_seconds=ai_video_max_wait_seconds,
        ai_video_supported_durations=ai_video_supported_durations,
        ai_video_supported_ratios=ai_video_supported_ratios,
        ai_video_supported_resolutions=ai_video_supported_resolutions,
        ffmpeg_path=os.getenv("CONTENT_FFMPEG_PATH", "").strip() or None,
        ffmpeg_timeout_seconds=_positive_float("CONTENT_FFMPEG_TIMEOUT_SECONDS", "600"),
        antivirus_command=os.getenv("CONTENT_ANTIVIRUS_COMMAND", "").strip() or None,
        antivirus_timeout_seconds=_positive_float("CONTENT_ANTIVIRUS_TIMEOUT_SECONDS", "60"),
        antivirus_required=_boolean("CONTENT_ANTIVIRUS_REQUIRED"),
        project_media_quota_bytes=project_media_quota_bytes,
    )
