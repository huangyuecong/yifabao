from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from . import responses_fallback
from .generation_errors import safe_provider_error_metadata
from .publishable_text import clean_text_content
from .providers import (
    ContentGenerationInput,
    ProviderStatus,
    TextGenerationResult,
    selected_text_candidate,
    transform_required_fields,
    unchanged_transform_fields,
)


class ProviderCallError(RuntimeError):
    """Safe provider error that never includes credentials or request headers."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_call_failed",
        retryable: bool = False,
        status_code: int | None = None,
        provider_code: str | None = None,
        provider_request_id: str | None = None,
        provider_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_request_id = provider_request_id
        self.provider_status_code = provider_status_code


_MODEL_FAILOVER_STATUS_CODES = frozenset({408, 425, 429})


def _strip_code_fence(value: str) -> str:
    text = value.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, int)}


def _source_reference_ids(request: ContentGenerationInput) -> set[str]:
    if not isinstance(request.source_content, dict):
        return set()
    values = request.source_content.get("source_reference_ids", [])
    if not isinstance(values, list):
        return set()
    return {item for item in values if isinstance(item, str)}


class OpenAICompatibleTextProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        temperature: float,
        max_attempts: int,
    ):
        endpoint = base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.max_attempts = max_attempts
        self._request_overrides: dict[str, Any] = {}
        self.status = ProviderStatus(
            provider_id=provider_id,
            name="openai-compatible-text",
            capability="text-generation",
            mode="api",
            external=True,
            enabled=True,
            model=model,
        )
        self.capabilities = {
            "operations": [
                "topics",
                "copies",
                "transform_copy",
                "storyboard",
                "timeline_edits",
            ],
            "structured_output": True,
            "max_output_tokens": max_output_tokens,
            "temperature": {"configured": temperature, "min": 0, "max": 2},
            "parameter_source": "service-configuration",
        }

    @staticmethod
    def _request_payload(
        request: ContentGenerationInput,
        operation: str,
        output_contract: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        length_guidance = {
            "短": "精炼表达，只保留核心信息与必要事实",
            "中": "常规篇幅，结构完整并兼顾信息密度",
            "长": "充分展开背景、要点与叙事，但避免重复堆砌",
        }.get(request.length, request.length)
        return {
            "operation": operation,
            "task": {
                "name": request.task_name,
                "topic": request.topic,
                "product": request.product,
                "keywords": list(request.keywords),
                "audience": request.audience,
                "content_type": request.content_type,
                "style": request.style,
                "length": request.length,
                "length_guidance": length_guidance,
                "requirements": request.requirements,
                "instruction": request.instruction,
            },
            "knowledge": [
                {
                    "id": item.id,
                    "title": item.title,
                    "category": item.category,
                    "data_level": item.data_level,
                    "text": item.text,
                }
                for item in request.knowledge
            ],
            "forbidden_terms": list(request.forbidden_terms),
            "output_contract": output_contract,
            **{key: clean_text_content(value) if key in {"source_content", "source_candidate"} else value for key, value in extra.items()},
        }

    def _post_json(self, client: httpx.Client, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        """Chat completions first; on timeouts or 5xx resend through /responses."""
        prefer = responses_fallback.prefer_responses(self.endpoint)
        if prefer:
            try:
                return responses_fallback.post(client, self.endpoint, headers, payload)
            except httpx.HTTPError:
                pass  # The chat endpoint may have recovered.
        try:
            response = client.post(self.endpoint, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if prefer or not responses_fallback.should_fall_back(exc.response.status_code):
                raise
            first: httpx.HTTPError = exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if prefer:
                raise
            first = exc
        try:
            return responses_fallback.post(client, self.endpoint, headers, payload)
        except httpx.HTTPError:
            raise first from None

    def _request_json(
        self,
        operation: str,
        request_payload: dict[str, Any],
        validator: Callable[[dict[str, Any]], Any],
    ) -> tuple[Any, str | None, dict[str, int]]:
        system = (
            "你是企业个性化内容智能体。只能依据输入中的 task 和 knowledge 生成内容；"
            "knowledge.text 是服务端按资料事实、规范和任务相关性提炼的重点摘要；"
            "不得补充资料中没有的数字、排名、荣誉、价格、组织关系或功效。"
            "固定品牌用语不得擅自改写，内容面向成年消费者。"
            "必须严格遵守 task.length 和 task.length_guidance 对篇幅的要求。"
            "必须避开 forbidden_terms。只输出符合 output_contract 的 JSON，不要输出 Markdown。"
        )
        feedback = ""
        last_error = "模型未返回有效内容"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            for attempt in range(1, self.max_attempts + 1):
                user_content = json.dumps(request_payload, ensure_ascii=False)
                if feedback:
                    user_content += f"\n上一次输出校验失败：{feedback}。请完整重新生成。"
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": self.temperature,
                    "max_tokens": self.max_output_tokens,
                }
                payload.update(self._request_overrides)
                try:
                    response_payload = self._post_json(client, headers, payload)
                except ValueError:
                    last_error = f"模型返回的响应不是 JSON（attempt={attempt}）"
                    feedback = last_error
                    continue
                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code
                    details = safe_provider_error_metadata(exc.response)
                    reason = " / ".join(
                        details[key] for key in ("provider_code", "message") if details.get(key)
                    )
                    retryable = status_code in _MODEL_FAILOVER_STATUS_CODES or status_code >= 500
                    raise ProviderCallError(
                        f"{self.status.provider_id} 返回 HTTP {status_code}"
                        + (f"：{reason}" if reason else ""),
                        code=("provider_model_unavailable" if retryable else "provider_rejected"),
                        retryable=retryable,
                        status_code=status_code,
                        provider_code=details.get("provider_code"),
                        provider_request_id=details.get("provider_request_id"),
                        provider_status_code=status_code,
                    ) from exc
                except httpx.HTTPError as exc:
                    raise ProviderCallError(
                        f"{self.status.provider_id} 请求失败：{type(exc).__name__}",
                        code="provider_transport_error",
                        retryable=True,
                    ) from exc

                if not isinstance(response_payload, dict):
                    last_error = f"模型响应顶层不是 JSON 对象（attempt={attempt}）"
                    feedback = last_error
                    continue
                choices = response_payload.get("choices")
                choice = (
                    choices[0]
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                    else {}
                )
                message = choice.get("message")
                if not isinstance(message, dict):
                    message = {}
                content = message.get("content")
                finish_reason = choice.get("finish_reason")
                usage = _safe_usage(response_payload.get("usage"))
                if not isinstance(content, str) or not content.strip():
                    last_error = (
                        f"模型返回空内容（finish_reason={finish_reason}, attempt={attempt}）"
                    )
                    feedback = last_error
                    continue
                try:
                    decoded = json.loads(_strip_code_fence(content))
                    if not isinstance(decoded, dict):
                        raise TypeError("顶层必须是 JSON 对象")
                    validated = validator(decoded)
                except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                    completion_tokens = usage.get("completion_tokens", 0)
                    if finish_reason == "length" or completion_tokens >= self.max_output_tokens:
                        last_error = f"模型输出达到 {self.max_output_tokens} token 上限并被截断"
                        feedback = (
                            f"{last_error}。请缩短每条内容，优先保证 JSON 完整闭合，不得省略候选项"
                        )
                    else:
                        last_error = str(exc)
                        feedback = last_error
                    continue
                return (
                    validated,
                    finish_reason,
                    usage,
                )
        raise ProviderCallError(
            f"{self.status.provider_id} 执行 {operation} 连续 "
            f"{self.max_attempts} 次输出无效：{last_error}",
            code="provider_invalid_response",
            retryable=True,
        )

    def _result(
        self,
        content: Any,
        finish_reason: str | None,
        usage: dict[str, int],
    ) -> TextGenerationResult:
        return TextGenerationResult(
            content=content,
            provider_id=self.status.provider_id,
            model=self.model,
            finish_reason=finish_reason,
            usage=usage,
        )

    def topics(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        allowed_source_ids = {item.id for item in request.knowledge}
        request_payload = self._request_payload(
            request,
            "generate_topics",
            {
                "candidates": [
                    {
                        "direction": "内容方向",
                        "title": "候选标题",
                        "source_ids": ["实际使用的 knowledge.id"],
                    }
                ]
            },
            count=count,
        )

        def validate(value: dict[str, Any]) -> list[dict[str, Any]]:
            candidates = value.get("candidates")
            if not isinstance(candidates, list) or len(candidates) != count:
                raise ValueError(f"candidates 必须包含 {count} 条标题")
            normalized: list[dict[str, Any]] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise TypeError("每条标题必须是 JSON 对象")
                title = candidate.get("title")
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("每条标题都必须包含非空 title")
                source_ids = candidate.get("source_ids", [])
                if not isinstance(source_ids, list) or any(
                    item not in allowed_source_ids for item in source_ids
                ):
                    raise ValueError("标题包含无效 source_ids")
                if allowed_source_ids and not source_ids:
                    raise ValueError("使用知识资料生成标题时 source_ids 不能为空")
                normalized.append(
                    {
                        "direction": str(candidate.get("direction") or "").strip(),
                        "title": title.strip(),
                        "source_ids": list(dict.fromkeys(source_ids)),
                    }
                )
            return normalized

        content, finish_reason, usage = self._request_json(
            "generate_topics",
            request_payload,
            validate,
        )
        return self._result(content, finish_reason, usage)

    def copies(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        allowed_source_ids = {item.id for item in request.knowledge}
        request_payload = self._request_payload(
            request,
            "generate_copies",
            {
                "candidates": [
                    {
                        "title": "标题",
                        "body": "正文",
                        "source_ids": ["实际使用的 knowledge.id"],
                    }
                ]
            },
            count=count,
            source_content=request.source_content or {},
        )

        def validate(value: dict[str, Any]) -> list[dict[str, Any]]:
            candidates = value.get("candidates")
            if not isinstance(candidates, list) or len(candidates) != count:
                raise ValueError(f"candidates 必须包含 {count} 条文案")
            normalized: list[dict[str, Any]] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise TypeError("每条文案必须是 JSON 对象")
                for field_name in ("title", "body"):
                    if (
                        not isinstance(candidate.get(field_name), str)
                        or not candidate[field_name].strip()
                    ):
                        raise ValueError(f"文案缺少非空字段：{field_name}")
                source_ids = candidate.get("source_ids", [])
                if not isinstance(source_ids, list) or not all(
                    isinstance(item, str) for item in source_ids
                ):
                    raise ValueError("source_ids 必须是字符串数组")
                if any(item not in allowed_source_ids for item in source_ids):
                    raise ValueError("source_ids 包含未提供的知识资料")
                if allowed_source_ids and not source_ids:
                    raise ValueError("使用知识资料生成时 source_ids 不能为空")
                normalized.append(
                    {
                        "title": candidate["title"].strip(),
                        "body": candidate["body"].strip(),
                        "source_ids": list(dict.fromkeys(source_ids)),
                    }
                )
            return normalized

        content, finish_reason, usage = self._request_json(
            "generate_copies",
            request_payload,
            validate,
        )
        return self._result(content, finish_reason, usage)

    def transform_copy(
        self,
        request: ContentGenerationInput,
        operation: str,
    ) -> TextGenerationResult:
        allowed_source_ids = {item.id for item in request.knowledge} | _source_reference_ids(
            request
        )
        source_candidate = clean_text_content(selected_text_candidate(request.source_content))
        title_only = isinstance(source_candidate, dict) and "body" not in source_candidate
        must_change_fields = transform_required_fields(source_candidate, request.instruction)
        candidate_contract = (
            {
                "direction": "调整后的内容方向",
                "title": "调整后的标题",
                "source_ids": ["实际使用的 knowledge.id"],
            }
            if title_only
            else {
                "title": "调整后的标题",
                "body": "调整后的正文",
                "source_ids": ["实际使用的 knowledge.id"],
            }
        )
        request_payload = self._request_payload(
            request,
            "transform_copy",
            {"candidate": candidate_contract},
            transformation=operation,
            transformation_requirements={
                "instruction": request.instruction,
                "must_apply_transformation": True,
                "must_apply_instruction": bool(request.instruction.strip()),
                "must_change_fields": list(must_change_fields),
                "rule": (
                    "must_change_fields 的每个字段都必须与 source_candidate 实质不同，禁止原样返回"
                ),
            },
            source_candidate=source_candidate,
            source_content=request.source_content or {},
        )

        def validate(value: dict[str, Any]) -> dict[str, Any]:
            candidate = value.get("candidate")
            if not isinstance(candidate, dict):
                raise TypeError("缺少 candidate 对象")
            required_fields = ("title",) if title_only else ("title", "body")
            for field_name in required_fields:
                if (
                    not isinstance(candidate.get(field_name), str)
                    or not candidate[field_name].strip()
                ):
                    raise ValueError(f"调整结果缺少非空字段：{field_name}")
            source_ids = candidate.get("source_ids", [])
            if not isinstance(source_ids, list) or any(
                item not in allowed_source_ids for item in source_ids
            ):
                raise ValueError("调整结果包含无效 source_ids")
            if allowed_source_ids and not source_ids:
                raise ValueError("调整结果使用资料时 source_ids 不能为空")
            normalized = {
                "title": candidate["title"].strip(),
                "source_ids": list(dict.fromkeys(source_ids)),
            }
            if title_only:
                normalized["direction"] = str(candidate.get("direction") or "").strip()
            else:
                normalized["body"] = candidate["body"].strip()
            unchanged_fields = unchanged_transform_fields(
                source_candidate,
                normalized,
                instruction=request.instruction,
            )
            if unchanged_fields:
                field_labels = {"title": "标题", "body": "正文"}
                labels = "、".join(
                    field_labels.get(field_name, field_name) for field_name in unchanged_fields
                )
                raise ValueError(
                    f"调整结果的{labels}与原文相同；必须执行用户指令并实质改写这些字段"
                )
            return normalized

        content, finish_reason, usage = self._request_json(
            "transform_copy",
            request_payload,
            validate,
        )
        return self._result(content, finish_reason, usage)

    def storyboard(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        allowed_source_ids = {item.id for item in request.knowledge} | _source_reference_ids(
            request
        )
        expected_count = max(2, count)
        request_payload = self._request_payload(
            request,
            "generate_storyboard",
            {
                "shots": [
                    {
                        "sequence": 1,
                        "visual": "画面或素材说明",
                        "narration": "口播或旁白",
                        "subtitle": "字幕文本",
                        "duration_seconds": 3,
                        "transition": "none/fade",
                        "source_ids": ["实际使用的 knowledge.id"],
                    }
                ]
            },
            count=expected_count,
            source_content=request.source_content or {},
        )

        def validate(value: dict[str, Any]) -> list[dict[str, Any]]:
            shots = value.get("shots")
            if not isinstance(shots, list) or len(shots) != expected_count:
                raise ValueError(f"shots 必须包含 {expected_count} 个分镜")
            normalized: list[dict[str, Any]] = []
            for index, shot in enumerate(shots, start=1):
                if not isinstance(shot, dict):
                    raise TypeError("每个分镜必须是 JSON 对象")
                for field_name in ("visual", "narration", "subtitle"):
                    if not isinstance(shot.get(field_name), str):
                        raise TypeError(f"分镜缺少字段：{field_name}")
                duration = shot.get("duration_seconds", shot.get("duration", 3))
                if not isinstance(duration, (int, float)) or duration <= 0:
                    raise ValueError("分镜 duration_seconds 必须为正数")
                source_ids = shot.get("source_ids", [])
                if not isinstance(source_ids, list) or any(
                    item not in allowed_source_ids for item in source_ids
                ):
                    raise ValueError("分镜包含无效 source_ids")
                if allowed_source_ids and not source_ids:
                    raise ValueError("分镜使用资料时 source_ids 不能为空")
                normalized.append(
                    {
                        "sequence": index,
                        "visual": shot["visual"].strip(),
                        "narration": shot["narration"].strip(),
                        "subtitle": shot["subtitle"].strip(),
                        "duration_seconds": duration,
                        "transition": str(shot.get("transition", "none")),
                        "source_ids": list(dict.fromkeys(source_ids)),
                    }
                )
            return normalized

        content, finish_reason, usage = self._request_json(
            "generate_storyboard",
            request_payload,
            validate,
        )
        return self._result(content, finish_reason, usage)

    def timeline_edits(self, request: ContentGenerationInput) -> TextGenerationResult:
        source = request.source_content if isinstance(request.source_content, dict) else {}
        timeline = source.get("timeline") if isinstance(source.get("timeline"), dict) else {}
        tracks = timeline.get("tracks") if isinstance(timeline.get("tracks"), list) else []
        item_ids: set[str] = set()
        track_ids: set[str] = set()
        for track in tracks:
            if not isinstance(track, dict):
                continue
            track_id = track.get("id")
            if isinstance(track_id, str):
                track_ids.add(track_id)
            items = track.get("items")
            if not isinstance(items, list):
                continue
            item_ids.update(
                item["id"]
                for item in items
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            )

        request_payload = self._request_payload(
            request,
            "timeline_edits",
            {
                "operations": [
                    {
                        "op_id": "唯一操作 ID（最多 64 字符）",
                        "type": ("remove/move/trim/split/update/update_canvas/update_track"),
                        "item_id": "仅使用 source_timeline 中已有的 item.id",
                        "track_id": "仅使用 source_timeline 中已有的 track.id",
                        "changes": "仅使用对应操作允许的结构化字段",
                    }
                ]
            },
            source_timeline=timeline,
            target_item_ids=source.get("target_item_ids", []),
            constraints={
                "candidate_only": True,
                "do_not_add_assets": True,
                "do_not_return_file_paths_or_ffmpeg_expressions": True,
                "maximum_operations": 100,
            },
        )

        allowed_types = {
            "remove",
            "move",
            "trim",
            "split",
            "update",
            "update_canvas",
            "update_track",
        }
        item_operations = {"remove", "move", "trim", "split", "update"}
        allowed_item_changes = {
            "label",
            "start_seconds",
            "duration_seconds",
            "trim_in_seconds",
            "trim_out_seconds",
            "volume",
            "speed",
            "enabled",
            "content",
            "transform",
            "metadata",
        }
        allowed_canvas_changes = {"width", "height", "fps", "aspect_ratio"}
        allowed_track_changes = {"name", "muted", "locked"}

        def validate(value: dict[str, Any]) -> list[dict[str, Any]]:
            operations = value.get("operations")
            if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
                raise ValueError("operations 必须包含 1 至 100 个修改操作")
            normalized: list[dict[str, Any]] = []
            operation_ids: set[str] = set()
            for operation in operations:
                if not isinstance(operation, dict):
                    raise TypeError("每个时间线修改必须是 JSON 对象")
                operation_type = operation.get("type")
                operation_id = operation.get("op_id")
                if operation_type not in allowed_types:
                    raise ValueError("时间线修改包含不允许的操作类型")
                if (
                    not isinstance(operation_id, str)
                    or not operation_id.strip()
                    or len(operation_id) > 64
                    or operation_id in operation_ids
                ):
                    raise ValueError("每个操作必须有唯一且有效的 op_id")
                operation_ids.add(operation_id)
                item = dict(operation)
                item["op_id"] = operation_id.strip()
                if operation_type in item_operations:
                    item_id = operation.get("item_id")
                    if not isinstance(item_id, str) or item_id not in item_ids:
                        raise ValueError("操作引用了 source_timeline 之外的 item_id")
                if (
                    operation_type == "move"
                    and operation.get("track_id") is not None
                    and operation.get("track_id") not in track_ids
                ):
                    raise ValueError("move 引用了无效 track_id")
                if operation_type == "update_track" and operation.get("track_id") not in track_ids:
                    raise ValueError("update_track 引用了无效 track_id")
                if operation_type in {"update", "update_canvas", "update_track"}:
                    changes = operation.get("changes")
                    if not isinstance(changes, dict) or not changes:
                        raise ValueError("update 操作必须包含非空 changes")
                    allowed_changes = (
                        allowed_item_changes
                        if operation_type == "update"
                        else allowed_canvas_changes
                        if operation_type == "update_canvas"
                        else allowed_track_changes
                    )
                    if any(key not in allowed_changes for key in changes):
                        raise ValueError("changes 包含不允许的字段")
                normalized.append(item)
            return normalized

        content, finish_reason, usage = self._request_json(
            "timeline_edits",
            request_payload,
            validate,
        )
        return TextGenerationResult(
            content=content,
            provider_id=self.status.provider_id,
            model=self.model,
            finish_reason=finish_reason,
            usage=usage,
            prompt_version="timeline-edit-v1",
        )


class VolcengineArkTextGenerationPort(OpenAICompatibleTextProvider):
    """Ark Chat uses the same wire contract while retaining supplier identity at the edge."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self._request_overrides = {
            "reasoning_effort": "minimal",
            "response_format": {"type": "json_object"},
        }
        self.status = ProviderStatus(
            provider_id=self.status.provider_id,
            name="volcengine-ark-text",
            capability="text-generation",
            mode="api",
            external=True,
            enabled=True,
            model=self.model,
        )


class FailoverTextProvider:
    """Prefer one real text model and fail over per request to another real model."""

    def __init__(
        self,
        primary: OpenAICompatibleTextProvider,
        fallback: OpenAICompatibleTextProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.status = primary.status
        self.capabilities = {
            **primary.capabilities,
            "primary_model": primary.model,
            "fallback_model": fallback.model,
            "failover_policy": "per-request",
        }

    def _invoke(self, method_name: str, *args: Any) -> TextGenerationResult:
        try:
            return getattr(self.primary, method_name)(*args)
        except ProviderCallError as primary_error:
            if not primary_error.retryable:
                raise
        try:
            return getattr(self.fallback, method_name)(*args)
        except ProviderCallError as fallback_error:
            raise ProviderCallError(
                f"主模型 {self.primary.model} 失败后，兜底模型 "
                f"{self.fallback.model} 也不可用：{fallback_error}",
                code=fallback_error.code,
                retryable=fallback_error.retryable,
                status_code=fallback_error.status_code,
                provider_code=fallback_error.provider_code,
                provider_request_id=fallback_error.provider_request_id,
                provider_status_code=fallback_error.provider_status_code,
            ) from fallback_error

    def topics(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        return self._invoke("topics", request, count)

    def copies(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        return self._invoke("copies", request, count)

    def transform_copy(
        self,
        request: ContentGenerationInput,
        operation: str,
    ) -> TextGenerationResult:
        return self._invoke("transform_copy", request, operation)

    def storyboard(self, request: ContentGenerationInput, count: int) -> TextGenerationResult:
        return self._invoke("storyboard", request, count)

    def timeline_edits(self, request: ContentGenerationInput) -> TextGenerationResult:
        return self._invoke("timeline_edits", request)
