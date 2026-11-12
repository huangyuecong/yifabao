"""User-facing generation errors. Classification never decides provider retries."""

from __future__ import annotations

import re
from typing import Any

_HAN = re.compile(r"[\u3400-\u9fff]")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_LOCAL_CODES = {
    "provider_call_failed",
    "provider_rejected",
    "provider_model_unavailable",
    "provider_transport_error",
    "provider_invalid_response",
    "input_image_privacy_information",
    "generation_failed",
    "unknown_error",
    "content_input_rejected",
    "content_output_rejected",
    "content_rejected",
    "input_image_real_person",
    "input_video_real_person",
    "account_payment_required",
    "authentication_failed",
    "permission_denied",
    "model_not_found",
    "input_too_long",
    "invalid_parameters",
    "rate_limited",
    "timeout",
    "provider_unavailable",
    "network_error",
    "invalid_response",
    "reference_unavailable",
    "provider_transform_no_effect",
    "render_failed",
    "input_too_large",
    "unsupported_media_format",
}


def _safe_identifier(value: Any) -> str | None:
    # Reject, rather than truncate, identifiers: a partial request ID is misleading.
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not _TOKEN.fullmatch(text):
        return None
    if "..." in text:
        return None
    if re.match(
        r"(?i)(?:sk-|sk_|AKIA|Bearer:|Basic:|eyJ|(?:api[-_]?key|access[-_]?key|secret|token|authorization|password|signature):)",
        text,
    ):
        return None
    return text


def safe_generation_error_text(value: Any, *, limit: int = 800) -> str:
    """Retain diagnostic prose, never credentials, URLs, HTML or stack traces."""
    if not isinstance(value, str):
        return ""
    # Do not take a prefix of a huge body: it could cut a secret before its suffix.
    if len(value) > 32000:
        return ""
    if re.search(r"(?is)<\s*!?[a-z][a-z0-9:-]*(?:\s|/?>)", value):
        return ""
    text = re.split(r"(?i)Traceback\s*\(|\n\s*(?:File\s+\"|at\s+\w+[.$])", value)[0]
    text = re.sub(r"(?is)data:[^,\s]+,[A-Za-z0-9+/=%\s._-]+", "[媒体数据已隐藏]", text)
    text = re.sub(r"(?i)data:[^\s\"']+", "[媒体数据已隐藏]", text)
    text = re.sub(r"(?i)https?://[^\s<>\"']+", "[链接已隐藏]", text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"'<>]+", "[凭证已隐藏]", text)
    text = re.sub(
        r"(?i)\b(?:api[-_ ]?key|secret|token)\s+(?:provided|is)\s*:?\s*[^\s,;\"'<>]+",
        "[凭证已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)(?:[\"']?(?:api[-_ ]?key|access[-_ ]?key(?:[-_ ]?(?:id|secret))?|"
        r"secret(?:[-_ ]?key)?|token|authorization|cookie|signature|password|"
        r"密钥|凭证|密码)[\"']?)\s*[:=：]\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;，；<>]+)",
        "[凭证已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:sk[-_][A-Za-z0-9_-]{6,}|AKIA[A-Z0-9]{16}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b",
        "[凭证已隐藏]",
        text,
    )
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|/(?:home|users|var|tmp)/)[^\s,;\"']+", "[路径已隐藏]", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def safe_provider_error_metadata(response: Any) -> dict[str, Any]:
    """Read only whitelisted error fields from a provider HTTP response."""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    result: dict[str, Any] = {}
    if isinstance(payload, dict):
        metadata = payload.get("ResponseMetadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        error = payload.get("error") or metadata.get("Error")
        if not isinstance(error, dict):
            error = payload.get("detail")
        if not isinstance(error, dict):
            error = payload
        code = _safe_identifier(error.get("code") or error.get("Code"))
        if code:
            result["provider_code"] = code
        message = error.get("message") or error.get("Message")
        if not message and isinstance(payload.get("error"), str):
            message = payload["error"]
        if not message and isinstance(payload.get("detail"), str):
            message = payload["detail"]
        safe_message = safe_generation_error_text(message)
        if safe_message:
            result["message"] = safe_message
        request_id = (
            error.get("request_id")
            or error.get("requestId")
            or error.get("RequestId")
            or payload.get("request_id")
            or payload.get("requestId")
            or payload.get("RequestId")
            or metadata.get("RequestId")
        )
        safe_request_id = _safe_identifier(request_id)
        if safe_request_id:
            result["provider_request_id"] = safe_request_id
    elif payload is None:
        safe_message = safe_generation_error_text(getattr(response, "text", ""))
        if safe_message:
            result["message"] = safe_message
    headers = getattr(response, "headers", {})
    if "provider_request_id" not in result:
        for key in ("x-request-id", "x-tt-logid", "x-log-id", "request-id"):
            safe_request_id = _safe_identifier(headers.get(key))
            if safe_request_id:
                result["provider_request_id"] = safe_request_id
                break
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["provider_status_code"] = status
    return result


def _classify(reason: str) -> tuple[str, str, str, bool] | None:
    """Match explicit codes before prose and HTTP fallbacks."""
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", reason.lower())

    def contains(*terms: str) -> bool:
        return any(term in normalized for term in terms)

    media = (
        "图片"
        if contains("image", "图片")
        else "视频"
        if contains("video", "视频")
        else "文字"
        if contains("text", "prompt", "文字", "提示词")
        else "内容"
    )
    privacy = contains("privacyinformation", "realperson", "realpeople", "真人", "人脸隐私")
    if privacy:
        if contains("output", "generated", "输出", "生成结果"):
            return (
                "content_safety",
                "content_output_rejected",
                f"AI 生成的{media}可能涉及真人或个人隐私，未通过服务方审核，结果无法返回。请检查创作要求及参考素材后重新生成。",
                False,
            )
        code = "input_video_real_person" if media == "视频" else "input_image_real_person"
        return (
            "content_safety",
            code,
            f"AI 服务认为参考{media if media != '内容' else '图片'}可能涉及真人或个人隐私，未通过审核。请检查并更换相关参考素材后再试。",
            False,
        )
    if contains("copyright", "copyrightrestriction", "ipinfringement", "版权限制"):
        return (
            "content_safety",
            "content_output_rejected",
            "AI 服务认为本次生成的视频可能涉及版权限制，未返回成片。"
            "参考图和参考视频都支持上传，这次素材已经传到模型侧，不是地址过期。"
            "请换一段差异更大的参考视频，或改成更明确的再创作要求后再试；不要直接用刚生成的同一段视频去复现相近画面。",
            False,
        )
    moderation = contains(
        "sensitivecontent",
        "contentfilter",
        "contentpolicy",
        "safetyviolation",
        "moderation",
        "contentinputrejected",
        "contentoutputrejected",
        "contentrejected",
        "审核",
        "违规",
        "敏感内容",
    )
    if moderation:
        if contains("output", "generated", "输出", "生成结果"):
            return (
                "content_safety",
                "content_output_rejected",
                f"AI 生成的{media}未通过服务方的内容审核，结果无法返回。请检查创作要求及参考素材后重新生成。",
                False,
            )
        if contains("input", "prompt", "输入", "提交", "提示词"):
            return (
                "content_safety",
                "content_input_rejected",
                f"提交的{media}未通过 AI 服务的内容审核，本次生成被拒绝。请检查提示词及参考素材后再试。",
                False,
            )
        return (
            "content_safety",
            "content_rejected",
            "本次生成未通过 AI 服务的内容审核，服务方未说明是输入内容还是生成结果。请检查提示词及参考素材后再试。",
            False,
        )
    if contains(
        "accountoverdue",
        "insufficientbalance",
        "insufficientquota",
        "paymentrequired",
        "arrears",
        "余额不足",
        "欠费",
        "额度不足",
    ):
        return (
            "billing",
            "account_payment_required",
            "AI 服务账号余额或可用额度不足，暂时无法生成。请联系管理员充值或补充额度后再试。",
            False,
        )
    if contains(
        "invalidapikey",
        "invalidaccesskey",
        "authentication",
        "unauthorized",
        "signaturedoesnotmatch",
        "invalidsignature",
        "密钥无效",
        "认证失败",
        "密钥失效",
    ):
        return (
            "authentication",
            "authentication_failed",
            "AI 服务的访问凭证无效或已失效。请联系管理员检查服务密钥配置后再试。",
            False,
        )
    if contains(
        "permissiondenied",
        "accessdenied",
        "forbidden",
        "modelnotactivated",
        "没有权限",
        "无权访问",
        "未开通",
    ):
        return (
            "permission",
            "permission_denied",
            "当前 AI 服务账号没有使用该功能或模型的权限。请联系管理员检查模型是否已开通及访问权限。",
            False,
        )
    if contains("modelnotfound", "invalidmodel", "endpointnotfound", "模型不存在", "接入点不存在"):
        return (
            "model",
            "model_not_found",
            "配置的 AI 模型或接入地址不存在，或当前账号无法访问。请联系管理员检查模型名称和服务地址。",
            False,
        )
    if contains(
        "contextlengthexceeded",
        "maxtokensexceeded",
        "prompttoolong",
        "inputtoolong",
        "上下文长度",
        "提示词过长",
    ):
        return (
            "parameters",
            "input_too_long",
            "提示词或参考文字超过了当前模型的长度限制。请缩短内容后再试。",
            False,
        )
    if contains(
        "failedtodownload",
        "downloadfailed",
        "invalidurl",
        "urlinvalid",
        "imageurl",
        "videourl",
        "referenceunavailable",
        "素材地址无法",
        "无法下载参考",
        "参考素材不可",
        "无法处理参考图片或参考视频",
        "接收素材时返回了空",
    ):
        return (
            "reference",
            "reference_unavailable",
            "AI 服务无法读取参考素材。请检查参考图片和参考视频后重新上传再试。",
            False,
        )
    if contains(
        "requesttoolarge",
        "payloadtoolarge",
        "inputtoolarge",
        "http413",
        "requestentitytoolarge",
        "参考视频超过上传限制",
        "参考视频压缩",
    ):
        return (
            "parameters",
            "input_too_large",
            "参考视频或图片体积超过 AI 服务上传限制。请改用不超过 15 秒、体积更小的参考视频，或换更小的图片后再试。",
            False,
        )
    if contains("unsupportedmediatype", "unsupportedmediaformat"):
        return (
            "parameters",
            "unsupported_media_format",
            "参考素材的文件格式不受当前 AI 服务支持。请转换为该功能支持的格式后重新上传。",
            False,
        )
    if contains(
        "invalidparameter",
        "invalidargument",
        "badrequest",
        "validationerror",
        "unsupportedparameter",
        "missingparameter",
        "missingrequired",
        "requiredparameter",
        "参数不支持",
        "参数无效",
    ) or re.search(
        r"(?:参数|图片|视频|素材|时长|尺寸|比例|分辨率|清晰度|片段|镜头|标题|文案|提示词|裁剪|字幕).*(?:不能为空|不支持|不得|不能|必须|至少|最多|超过|无效)",
        reason,
    ):
        return (
            "parameters",
            "invalid_parameters",
            "提交的生成参数不符合当前 AI 模型要求。请检查提示词、时长、比例、清晰度及参考素材规格后再试。",
            False,
        )
    if contains("ratelimit", "toomanyrequests", "throttl", "限流", "请求过于频繁", "请求频率"):
        return (
            "rate_limit",
            "rate_limited",
            "AI 服务当前请求过多，已达到调用频率或并发上限。请稍等后重试。",
            True,
        )
    if contains("timeout", "timedout", "超时"):
        return (
            "timeout",
            "timeout",
            "等待 AI 服务响应超时，暂未取得生成结果。请稍后重试；如已有生成任务，可先查看任务状态。",
            True,
        )
    if contains(
        "connectionerror",
        "connecterror",
        "connectionreset",
        "networkerror",
        "transporterror",
        "remoteprotocolerror",
        "dns",
        "连接失败",
        "网络连接中断",
    ):
        return (
            "network",
            "network_error",
            "当前无法正常连接 AI 服务，可能是网络连接中断或服务地址不可达。请稍后重试；持续失败时请联系管理员检查网络及服务地址。",
            True,
        )
    if contains("transformnoeffect", "与原内容相同", "与原文相同", "修改要求未生效"):
        return (
            "response",
            "provider_transform_no_effect",
            "AI 返回的内容与原内容相同，未按要求完成修改。请补充明确的修改要求后重试。",
            True,
        )
    if contains("文件上传服务", "参考素材下载失败", "文件上传未返回"):
        return (
            "reference",
            "reference_unavailable",
            "参考视频或参考图没有成功传到 AI 服务。请稍后重试，或更换参考素材后再生成。",
            True,
        )
    if contains("空错误") and re.search(r"http\s*5\d\d", reason, re.I):
        return (
            "service",
            "provider_unavailable",
            "AI 服务暂时无法正常处理请求，未返回可用结果。请稍后重试；持续失败时请联系管理员检查服务状态。",
            True,
        )
    if contains(
        "invalidresponse",
        "输出无效",
        "响应不是json",
        "响应顶层不是",
        "返回空内容",
        "返回内容为空",
        "返回的任务结构无效",
        "缺少任务id",
        "返回的图片结果无效",
        "无法解析",
        "空错误",
    ):
        return (
            "response",
            "invalid_response",
            "AI 服务返回的内容为空、格式不正确或缺少必要结果，系统无法使用。请重试；持续失败时请联系管理员检查模型响应。",
            True,
        )
    if contains("renderfailed", "ffmpegfailed", "ffmpegerror", "视频合成失败"):
        return (
            "render",
            "render_failed",
            "视频合成失败，未生成可用成片。请检查素材是否能正常播放、片段裁剪范围及导出设置后重试；持续失败时请联系管理员排查合成服务。",
            True,
        )
    if contains(
        "serviceunavailable",
        "providerunavailable",
        "internalservererror",
        "internalerror",
        "badgateway",
        "providermodelunavailable",
        "服务不可用",
        "服务繁忙",
    ):
        return (
            "service",
            "provider_unavailable",
            "AI 服务暂时无法正常处理请求，未返回可用结果。请稍后重试；持续失败时请联系管理员检查服务状态。",
            True,
        )
    return None


def _parameter_reason(raw: str) -> str | None:
    """Translate identifiable constraints without inventing a provider limit."""
    lowered = raw.lower()
    fields = (
        (
            r"(?:number|count|max(?:imum)?)\s*(?:of\s*)?(?:reference[_ ]*)?images?|reference[_ .-]*images?",
            "参考图片数量",
            "张",
        ),
        (
            r"(?:number|count|max(?:imum)?)\s*(?:of\s*)?(?:reference[_ ]*)?videos?|reference[_ .-]*videos?",
            "参考视频数量",
            "段",
        ),
        (r"duration", "生成时长", "秒"),
        (r"resolution", "输出清晰度", ""),
        (r"aspect[_ .-]*ratio|\bratio\b", "画面比例", ""),
        (r"\bwidth\b", "图片宽度", "像素"),
        (r"\bheight\b", "图片高度", "像素"),
        (r"image[_ .-]*size|\bsize\b|pixels?", "图片尺寸", "像素"),
        (r"\bfps\b|frame[_ ]*rate", "视频帧率", "帧/秒"),
        (r"\bprompt\b", "提示词", ""),
    )
    field = next(
        ((label, unit) for pattern, label, unit in fields if re.search(pattern, lowered)), None
    )
    missing = re.search(r"\brequired\b|\bmissing\b|must be provided", lowered)
    if missing:
        return f"提交内容缺少{field[0] if field else '当前模型要求的必要参数'}。请补充后再试。"
    if not field:
        return None
    label, unit = field
    message = f"{label}不符合当前 AI 模型要求。"
    number = r"(\d{1,9}(?:\.\d{1,4})?)"
    limits = re.search(
        r"(?:between|range\s*(?:is|of)?\s*[:=]?)\s*[\[(]?\s*"
        + number
        + r"\s*(?:and|to|[-–~,])\s*"
        + number,
        lowered,
    )
    choices = re.search(
        r"(?:one of|supported (?:values|sizes|ratios|resolutions)(?: are)?|allowed values(?: are)?)\s*[:=]?\s*([\[\]0-9kKpPxX×:.,\s'\"/-]+)",
        raw,
        re.IGNORECASE,
    )
    maximum = re.search(
        r"(?:at most|maximum(?:\s+(?:is|of))?|must not exceed|no more than|<=)\s*[:=]?\s*" + number,
        lowered,
    )
    minimum = re.search(r"(?:at least|minimum(?:\s+(?:is|of))?|>=)\s*[:=]?\s*" + number, lowered)
    if limits:
        message += f"服务要求范围为 {limits[1]} 至 {limits[2]}{unit}。"
    elif choices:
        values = re.findall(r"\d{1,5}(?:[xX×:]\d{1,5}|[kKpP])?", choices[1])
        if values:
            message += f"可用值为 {'、'.join(values[:20])}。"
    elif maximum:
        message += f"最多允许 {maximum[1]}{unit}。"
    elif minimum:
        message += f"至少需要 {minimum[1]}{unit}。"
    return message + "请调整后再试。"


def _chinese_reason(text: str) -> str:
    text = safe_generation_error_text(text)
    text = re.sub(r"^(?:[\w.]+Error:\s*)+", "", text)
    text = re.sub(r"^.{0,160}?返回\s*HTTP\s+\d{3}\s*[：:]?\s*", "", text)
    text = re.sub(r"^.*(?:生成失败|调用失败|请求失败|任务创建失败)[：:]\s*", "", text)
    text = re.sub(
        r"^(?:AI\s*)?(?:服务|供应商|接口|模型)?(?:返回(?:错误|异常)?|失败|错误)[：:]\s*", "", text
    )
    text = re.sub(r"^[A-Za-z0-9_.-]+\s*[/：:]\s*", "", text)
    text = re.sub(r"(?i)\brequest[_ -]?id\s*[:=]\s*[^\s,;]+", "", text).strip()
    if not _HAN.search(text):
        return ""
    # Chinese wrappers such as “服务返回：<English body>” are not translated detail.
    if re.search(r"[A-Za-z][A-Za-z0-9_]*(?:\s+[A-Za-z][A-Za-z0-9_]*){3,}", text):
        return ""
    if re.search(
        r"\b(?:ProviderCallError|HTTPStatusError|source_ids|finish_reason|attempt|Traceback)\b|[{}]",
        text,
    ):
        return ""
    # A redaction marker alone is not a user-facing explanation.
    remaining = re.sub(r"\[(?:媒体数据|链接|凭证|路径)已隐藏\]", "", text)
    return text if _HAN.search(remaining) else ""


def _message_request_id(value: str) -> str | None:
    if len(value) > 32000:
        return None
    match = re.search(r"(?i)\brequest[_ -]?id\s*[:=]\s*([^\s,;，；<>\"']+)", value)
    return _safe_identifier(match[1]) if match else None


def describe_generation_error(
    error: Exception | str,
    *,
    error_code: str | None = None,
    status_code: int | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """Describe an existing failure without changing retry/failover behavior."""
    raw = safe_generation_error_text(str(error))
    local_code = error_code or getattr(error, "code", None)
    provider_code = _safe_identifier(getattr(error, "provider_code", None))
    if not provider_code and local_code not in _LOCAL_CODES:
        provider_code = _safe_identifier(local_code)
    request_id = _safe_identifier(
        getattr(error, "provider_request_id", None)
    ) or _message_request_id(str(error))
    provider_status = getattr(error, "provider_status_code", None)
    if provider_status is None:
        provider_status = (
            status_code if status_code is not None else getattr(error, "status_code", None)
        )
    if (
        not isinstance(provider_status, int)
        or isinstance(provider_status, bool)
        or not 100 <= provider_status <= 599
    ):
        provider_status = None
    if provider_status is None:
        match = re.search(r"\bHTTP\s+(\d{3})\b", raw, re.IGNORECASE)
        provider_status = int(match[1]) if match and 100 <= int(match[1]) <= 599 else None
    classification = (
        _classify(provider_code or "")
        or _classify(raw)
        or _classify(str(local_code or ""))
        or _classify(type(error).__name__)
    )
    if classification is None and provider_status is not None:
        reason = {
            400: "InvalidParameter",
            401: "InvalidApiKey",
            402: "AccountOverdue",
            403: "AccessDenied",
            404: "ModelNotFound",
            408: "Timeout",
            413: "PayloadTooLarge",
            415: "UnsupportedMediaType",
            422: "InvalidParameter",
            425: "TooManyRequests",
            429: "TooManyRequests",
            500: "InternalServerError",
            502: "BadGateway",
            503: "ServiceUnavailable",
            504: "Timeout",
        }.get(provider_status)
        classification = _classify(reason) if reason else None
    if classification is None:
        classification = (
            "unknown",
            "unknown_error",
            "本次生成失败，AI 服务未提供可识别的具体原因。"
            + (
                "请联系管理员凭错误编号排查。"
                if provider_code or request_id
                else "请联系管理员查看服务日志排查。"
            ),
            False,
        )
    category, code, message, suggested_retryable = classification
    chinese = _chinese_reason(raw)
    if chinese and category in {"parameters", "unknown"} and code not in {"input_too_large"}:
        message = chinese
    elif code == "invalid_parameters":
        message = _parameter_reason(raw) or message
    if code == "provider_transform_no_effect" and chinese:
        message = chinese
    if code == "invalid_response":
        if re.search(r"(?:达到.*(?:token|长度).*上限|finish_reason\s*[=:]\s*length|被截断)", raw):
            message = "AI 输出超过模型长度上限，结果被截断，无法形成完整内容。请缩短创作要求或篇幅后重试。"
        elif re.search(
            r"(?:标题|title).*(?:为空|不能为空|缺少|缺失|必须|missing|required)", raw, re.IGNORECASE
        ):
            message = "AI 未返回有效的标题，无法使用本次结果。请重试；持续失败时请联系管理员检查模型响应。"
        elif re.search(
            r"(?:正文|body).*(?:为空|不能为空|缺少|缺失|必须|missing|required)", raw, re.IGNORECASE
        ):
            message = "AI 未返回有效的文案正文，无法使用本次结果。请重试；持续失败时请联系管理员检查模型响应。"
    if code == "unknown_error" and provider_code:
        message += f"（服务错误码：{provider_code}）"
    explicit_retryable = retryable if retryable is not None else getattr(error, "retryable", None)
    result: dict[str, Any] = {
        "message": message,
        "category": category,
        "code": code,
        "retryable": explicit_retryable
        if isinstance(explicit_retryable, bool)
        else suggested_retryable,
    }
    if provider_code:
        result["provider_code"] = provider_code
    if request_id:
        result["provider_request_id"] = request_id
    if provider_status is not None:
        result["provider_status_code"] = provider_status
    return result
