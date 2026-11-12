"""Permission-scoped model configuration; credentials never leave the server."""

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from agent_core import Identity

from .access_control import get_content_identity as get_identity
from .config import Settings
from .entry_management import management_access


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    kind: str = "text"
    required: bool = True


def f(key, label, kind="text", required=True):
    return ConfigField(key, label, kind, required)


HIAGENT = (
    f("hiagent_base_url", "HiAgent 接口地址", "url"),
    f("hiagent_access_key", "Access Key", "secret"),
    f("hiagent_secret_key", "Secret Key", "secret"),
    f("hiagent_workspace_id", "工作空间 ID"),
)
ADAPTERS = {
    "text": {
        "openai_compatible": "OpenAI 兼容接口（含 GPUStack）",
        "volcengine_ark": "火山方舟文本接口",
    },
    "image": {
        "openai_compatible": "OpenAI 兼容图片接口（GPT Image）",
        "openai_responses": "OpenAI Responses 生图工具（ChatGPT 账号中转等）",
        "hiagent": "HiAgent 图片接口",
        "volcengine_ark": "火山方舟图片接口",
    },
    "video": {"hiagent": "HiAgent 视频接口", "volcengine_ark": "火山方舟视频接口"},
    "speech": {
        "chat-tts": "OpenAI 兼容配音（含 chat-tts）",
        "volcengine_tts_api_key": "豆包配音 API Key",
        "volcengine_tts": "豆包配音 Access Token",
    },
    "music": {
        "seedance": "火山方舟视频配乐",
        "hiagent_seedance": "HiAgent 视频配乐",
        "volcengine": "火山引擎背景音乐",
        "minimax": "MiniMax 音乐接口",
    },
}
LABELS = {"text": "文案", "image": "图片", "video": "视频", "speech": "配音", "music": "背景音乐"}
PREFIXES = {
    "text": "llm",
    "image": "image",
    "video": "ai_video",
    "speech": "speech",
    "music": "music",
}
MODEL_KEYS = {cap: f"{prefix}_model" for cap, prefix in PREFIXES.items()} | {
    "speech": "speech_resource_id"
}


def config_fields(capability: str, provider: str) -> tuple[ConfigField, ...]:
    prefix = PREFIXES[capability]
    hiagent = provider in {"hiagent", "hiagent_seedance"}
    result = list(HIAGENT if hiagent else (f(f"{prefix}_base_url", "接口地址", "url"),))
    if not hiagent:
        if provider == "volcengine_tts":
            result += [
                f("speech_app_id", "应用 ID"),
                f("speech_access_token", "Access Token", "secret"),
            ]
        elif provider == "volcengine":
            result += [
                f("music_access_key", "Access Key", "secret"),
                f("music_secret_key", "Secret Key", "secret"),
            ]
        else:
            result += [f(f"{prefix}_api_key", "API Key", "secret")]
    result += [f(MODEL_KEYS[capability], "模型服务 ID" if hiagent else "模型")]
    if capability == "text":
        result += [f("llm_fallback_model", "备用模型", required=False)]
    if hiagent and capability in {"video", "music"}:
        result += [f("hiagent_video_fallback_model", "备用模型服务 ID", required=False)]
    if capability == "speech":
        result += [
            f("speech_voice_type", "默认音色"),
            f("speech_supported_voices", "可用音色（逗号分隔）", "list"),
        ]
    if capability == "image" and provider == "openai_responses":
        result += [f("image_responses_model", "调用模型（负责调用生图工具的对话模型）")]
    if capability == "image" and not hiagent:
        result += [f("image_fallback_models", "备用模型（主模型失败时依次尝试）", "models", False)]
    if capability == "image":
        result += [
            f("image_supported_sizes", "可用尺寸（逗号分隔）", "list", False),
            f("image_supported_ratios", "可用比例（逗号分隔）", "list", False),
        ]
    if capability == "video":
        result += [
            f("ai_video_supported_durations", "时长秒数（逗号分隔）", "integers"),
            f("ai_video_supported_ratios", "可用比例（逗号分隔）", "list"),
            f("ai_video_supported_resolutions", "可用分辨率（逗号分隔）", "list"),
        ]
    return tuple(result)


def adapter_defaults(base: Settings, capability: str, provider: str) -> dict:
    """Suggest non-secret parameter ranges; never copy credentials across adapters."""
    defaults = {}
    for field in config_fields(capability, provider):
        if field.kind in {"list", "integers"} or field.key == "speech_voice_type":
            defaults[field.key] = getattr(base, field.key)
    return defaults


def require_manager(request: Request, identity: Identity = Depends(get_identity)) -> Identity:
    access = management_access(identity, request)
    if not access.can_manage:
        raise HTTPException(403, access.message)
    return identity


def configuration_path(base: Settings) -> Path:
    return base.data_dir / "private" / "ai-models.json"


def read_document(base: Settings) -> dict:
    path = configuration_path(base)
    if not path.exists():
        return {"revision": 0, "overrides": {}, "previous": {}, "events": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or type(document.get("revision")) is not int
            or not isinstance(document.get("overrides"), dict)
            or not isinstance(document.get("previous"), dict)
            or not isinstance(document.get("events"), list)
        ):
            raise ValueError()
        return document
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, "模型配置文件无法读取，请联系管理员恢复配置") from None


def _apply(base: Settings, capability: str, values: dict) -> Settings:
    prefix = PREFIXES[capability]
    provider_key = f"{prefix}_provider"
    provider = values.get(provider_key, getattr(base, provider_key))
    if not values:
        return base
    if provider not in ADAPTERS[capability]:
        raise ValueError("不支持的模型接口")
    fields = config_fields(capability, provider)
    allowed = {field.key for field in fields} | {provider_key}
    if set(values) - allowed:
        raise ValueError("包含不允许修改的配置项")
    typed = dict(values)
    for field in fields:
        if field.kind in {"list", "integers", "models"} and field.key in typed:
            typed[field.key] = tuple(typed[field.key])
    if provider != getattr(base, provider_key):
        typed[f"{prefix}_provider_id"] = f"{provider}-{capability}"
        if capability == "music":
            minimum, maximum = (
                (5, 15)
                if provider in {"seedance", "hiagent_seedance"}
                else (30, 120)
                if provider == "volcengine"
                else (1, 300)
            )
            typed.update(
                music_min_duration_seconds=minimum,
                music_max_duration_seconds=maximum,
                music_allowed_download_hosts=(),
            )
    if provider == "hiagent" and capability in {"image", "video"}:
        # Prevent an old per-capability key from shadowing the newly supplied AK.
        typed[f"{prefix}_api_key"] = typed.get("hiagent_access_key", base.hiagent_access_key)
    return replace(base, **typed)


def model_settings(base: Settings, capability: str) -> Settings:
    try:
        values = read_document(base)["overrides"].get(capability, {})
        return _apply(base, capability, values)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(503, "模型配置无效，请联系模型管理员恢复上一版") from None


def _url(value: str, *, hiagent=False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("接口地址必须是无用户名、密码和查询参数的 HTTP(S) 地址")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("接口端口无效") from None
    if hiagent and (parsed.scheme != "https" or parsed.path not in {"", "/"}):
        raise ValueError("HiAgent 地址需为 HTTPS 网关地址，不包含路径")
    return value.rstrip("/")


def validated_values(
    base: Settings, capability: str, provider: str, supplied: dict, *, for_listing=False
) -> dict:
    if capability not in ADAPTERS or provider not in ADAPTERS[capability]:
        raise ValueError("请选择支持的模型接口")
    fields = config_fields(capability, provider)
    if set(supplied) - {field.key for field in fields}:
        raise ValueError("包含不允许修改的配置项")
    prefix = PREFIXES[capability]
    provider_key = f"{prefix}_provider"
    values = {provider_key: provider}
    same_provider = provider == getattr(base, provider_key)
    if same_provider and provider == "hiagent" and capability in {"image", "video"}:
        base = replace(
            base, hiagent_access_key=getattr(base, f"{prefix}_api_key") or base.hiagent_access_key
        )
    for field in fields:
        value = supplied.get(field.key)
        if value is None or (field.kind == "secret" and value == ""):
            # Preserve existing secrets only on the same endpoint and adapter.
            value = getattr(base, field.key) if same_provider else None
            if field.kind == "secret" and value:
                endpoint_key = (
                    "hiagent_base_url"
                    if provider in {"hiagent", "hiagent_seedance"}
                    else f"{prefix}_base_url"
                )
                original = getattr(base, endpoint_key)
                requested = supplied.get(endpoint_key, original)
                if requested and str(requested).rstrip("/") != str(original or "").rstrip("/"):
                    raise ValueError("更换接口地址后，请重新填写密钥")
        if (
            for_listing
            and field.kind != "secret"
            and field.key
            not in {
                f"{prefix}_base_url",
                "hiagent_base_url",
                "hiagent_workspace_id",
                "speech_app_id",
            }
        ):
            continue
        if field.kind in {"list", "integers", "models"}:
            if value is None and not field.required:
                value = []
            if isinstance(value, str):
                value = [
                    item.strip() for item in value.replace("，", ",").split(",") if item.strip()
                ]
            if not isinstance(value, (tuple, list)) or len(value) > 100:
                raise ValueError(f"请填写{field.label}")
            if field.kind == "integers":
                try:
                    value = [int(item) for item in value]
                except (ValueError, TypeError):
                    raise ValueError("时长必须为整数秒") from None
                if any(not 5 <= item <= 120 for item in value):
                    raise ValueError("视频时长应在 5 到 120 秒之间")
            elif any(
                not isinstance(item, str) or not item.strip() or len(item) > 128 for item in value
            ):
                raise ValueError(f"{field.label}格式不正确")
            value = list(dict.fromkeys(value))
        else:
            if value is None:
                value = ""
            if not isinstance(value, str) or len(value) > (
                8192 if field.kind == "secret" else 2048
            ):
                raise ValueError(f"{field.label}格式不正确")
            value = value.strip()
            if any(ord(char) < 32 for char in value):
                raise ValueError(f"{field.label}不能包含控制字符")
            if field.kind == "url" and value:
                value = _url(value, hiagent=provider in {"hiagent", "hiagent_seedance"})
        if field.required and not value:
            raise ValueError(f"请填写{field.label}")
        values[field.key] = value
    if not for_listing:
        if capability == "text" and values.get("llm_model") == values.get("llm_fallback_model"):
            raise ValueError("主模型和备用模型不能相同")
        if capability == "image" and values.get("image_model") in values.get("image_fallback_models", []):
            raise ValueError("备用模型里不要再填主模型")
        if (
            capability == "speech"
            and values["speech_voice_type"] not in values["speech_supported_voices"]
        ):
            raise ValueError("默认音色必须在可用音色中")
    return values


_lock = RLock()


@contextmanager
def locked_document(base: Settings):
    path = configuration_path(base)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            if os.name == "nt":
                import msvcrt

                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield read_document(base)
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def write_document(base: Settings, document: dict) -> None:
    path = configuration_path(base)
    descriptor, temporary = tempfile.mkstemp(prefix=".ai-models-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(document, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _base() -> Settings:
    from .provider_dependencies import get_runtime_settings

    return get_runtime_settings()


def config_response(base: Settings, document: dict) -> dict:
    configurations = []
    for cap in ADAPTERS:
        current = _apply(base, cap, document["overrides"].get(cap, {}))
        provider = getattr(current, f"{PREFIXES[cap]}_provider")
        if provider == "hiagent" and cap in {"image", "video"}:
            current = replace(
                current,
                hiagent_access_key=getattr(current, f"{PREFIXES[cap]}_api_key")
                or current.hiagent_access_key,
            )
        values, configured_secrets = {}, []
        for field in config_fields(cap, provider):
            value = getattr(current, field.key)
            if field.kind == "secret":
                if value:
                    configured_secrets.append(field.key)
            else:
                values[field.key] = value
        configurations.append(
            {
                "capability": cap,
                "label": LABELS[cap],
                "provider": provider,
                "values": values,
                "configured_secrets": configured_secrets,
                "can_rollback": cap in document["previous"],
                "adapters": [
                    {
                        "value": name,
                        "label": label,
                        "fields": [asdict(field) for field in config_fields(cap, name)],
                        "defaults": adapter_defaults(base, cap, name),
                    }
                    for name, label in ADAPTERS[cap].items()
                ],
            }
        )
    return {
        "revision": document["revision"],
        "configurations": configurations,
        "history": document["events"][-30:][::-1],
    }


class ModelConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(max_length=64)
    values: dict[str, str | list[str] | list[int] | None] = Field(
        default_factory=dict, max_length=30
    )
    expected_revision: int = Field(ge=0)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


def write_header(request: Request):
    if request.headers.get("X-Content-Config-Request") != "1":
        raise HTTPException(403, "配置请求校验失败，请刷新页面重试")


class PrivateConfigRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # Default validation errors echo inputs, including mistyped secrets.
                raise HTTPException(422, "模型配置请求格式不正确，请检查填写内容") from None
            response.headers["Cache-Control"] = "private, no-store"
            return response

        return private_handler


router = APIRouter(
    prefix="/api/v1/ai-models", tags=["AI model configuration"], route_class=PrivateConfigRoute
)


@router.get("/access")
def access(
    request: Request, response: Response, identity: Identity = Depends(get_identity)
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    return asdict(management_access(identity, request))


@router.get("")
def get_configuration(response: Response, _: Identity = Depends(require_manager)) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    base = _base()
    try:
        return config_response(base, read_document(base))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(503, "模型配置无效，请联系管理员恢复配置文件") from None


def video_jobs_active() -> bool:
    # Poll/recovery/cancel requests resolve providers in separate requests. Keep
    # their credentials stable until all existing durable video jobs finish.
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import Job

    with SessionLocal() as session:
        return (
            session.scalar(
                select(Job.id)
                .where(
                    Job.kind.in_(("ai_video_generation", "video_project_ai_generation")),
                    Job.status.in_(("queued", "running")),
                )
                .limit(1)
            )
            is not None
        )


def _change(capability, payload, identity, *, rollback=False):
    if capability not in ADAPTERS:
        raise HTTPException(404, "该能力不支持模型配置")
    base = _base()
    try:
        with locked_document(base) as document:
            if payload.expected_revision != document["revision"]:
                raise HTTPException(409, "模型配置已被其他人更新，请重新加载后修改")
            if capability == "video" and video_jobs_active():
                raise HTTPException(409, "还有视频正在排队或生成，请待任务结束后再修改视频模型配置")
            prior = document["overrides"].get(capability)
            if rollback:
                if capability not in document["previous"]:
                    raise HTTPException(409, "暂无可恢复的上一版配置")
                next_values = document["previous"].pop(capability)
            else:
                current = _apply(base, capability, prior or {})
                next_values = validated_values(
                    current, capability, payload.provider, payload.values
                )
                document["previous"][capability] = prior
            if next_values is None:
                document["overrides"].pop(capability, None)
            else:
                _apply(base, capability, next_values)
                document["overrides"][capability] = next_values
            document["revision"] += 1
            document["events"].append(
                {
                    "revision": document["revision"],
                    "capability": capability,
                    "action": "rollback" if rollback else "saved",
                    "actor": identity.user_id,
                    "name": identity.name,
                    "at": datetime.now(UTC).isoformat(),
                    "fields": [] if rollback else sorted(payload.values),
                }
            )
            document["events"] = document["events"][-200:]
            result = config_response(base, document)
            write_document(base, document)
            return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except OSError:
        raise HTTPException(503, "配置保存失败，原配置未变更，请检查服务器存储") from None


@router.put("/{capability}", dependencies=[Depends(write_header)])
def save_configuration(
    capability: str, payload: ModelConfigRequest, identity: Identity = Depends(require_manager)
) -> dict:
    return _change(capability, payload, identity)


@router.post("/{capability}/rollback", dependencies=[Depends(write_header)])
def rollback_configuration(
    capability: str, payload: RollbackRequest, identity: Identity = Depends(require_manager)
) -> dict:
    return _change(capability, payload, identity, rollback=True)


def discover_models(settings: Settings, capability: str, provider: str) -> list[dict]:
    if provider in {"hiagent", "hiagent_seedance"}:
        from .hiagent_adapters import HiAgentClient

        parsed = urlsplit(settings.hiagent_base_url)
        client = HiAgentClient(
            access_key=settings.hiagent_access_key or "",
            secret_key=settings.hiagent_secret_key or "",
            workspace_id=settings.hiagent_workspace_id,
            host=parsed.hostname,
            port=parsed.port or 443,
            timeout=15,
        )
        items = client.list_granted_models()
        return [
            {
                "id": str(item.get("ID") or item.get("ModelServiceID") or item.get("ModelId")),
                "name": str(item.get("Name") or item.get("ModelName") or item.get("ID") or "模型"),
            }
            for item in items
            if item.get("ID") or item.get("ModelServiceID") or item.get("ModelId")
        ]
    if provider in {"minimax", "volcengine", "volcengine_tts", "volcengine_tts_api_key"}:
        raise HTTPException(422, "该接口未提供通用模型列表，请填写服务商提供的模型或资源 ID")
    prefix = PREFIXES[capability]
    endpoint = str(getattr(settings, f"{prefix}_base_url") or "").rstrip("/")
    if provider == "chat-tts" and not endpoint.endswith("/v1"):
        endpoint += "/v1"
    with (
        httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client,
        client.stream(
            "GET",
            endpoint + "/models",
            headers={"Authorization": f"Bearer {getattr(settings, f'{prefix}_api_key')}"},
        ) as response,
    ):
        if response.status_code in {401, 403}:
            raise HTTPException(422, "密钥校验失败或没有模型列表权限")
        if response.status_code != 200:
            raise HTTPException(422, "无法获取模型列表，请检查接口地址，或填写服务商提供的模型 ID")
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise ValueError("模型列表响应过大")
    document = json.loads(raw)
    rows = document.get("data", [])
    if not isinstance(rows, list):
        raise ValueError("模型列表格式不支持")
    return [
        {"id": item["id"], "name": str(item.get("name") or item["id"])}
        for item in rows
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ][:1000]


@router.post("/{capability}/models", dependencies=[Depends(write_header)])
def list_models(
    capability: str,
    payload: ModelConfigRequest,
    response: Response,
    _: Identity = Depends(require_manager),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    if capability not in ADAPTERS:
        raise HTTPException(404, "该能力不支持模型配置")
    try:
        base = _base()
        document = read_document(base)
        if payload.expected_revision != document["revision"]:
            raise HTTPException(409, "模型配置已更新，请重新加载后获取列表")
        current = _apply(base, capability, document["overrides"].get(capability, {}))
        values = validated_values(
            current, capability, payload.provider, payload.values, for_listing=True
        )
        models = discover_models(_apply(current, capability, values), capability, payload.provider)
        return {
            "models": models,
            "message": "已获取模型列表；请选用支持当前功能的模型，生成效果需实际验证",
        }
    except HTTPException:
        raise
    except Exception:
        # Upstream exceptions can embed credentials, URLs and response bodies.
        raise HTTPException(
            422, "模型列表获取失败，请检查接口、密钥和网络；也可填写服务商提供的模型 ID"
        ) from None
