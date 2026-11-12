import json

import httpx
import pytest

from content_service import llm_adapters, media_adapters
from content_service.generation_errors import (
    describe_generation_error,
    safe_generation_error_text,
    safe_provider_error_metadata,
)
from content_service.llm_adapters import (
    FailoverTextProvider,
    OpenAICompatibleTextProvider,
    ProviderCallError,
)


@pytest.mark.parametrize(
    ("provider_code", "expected", "phrase"),
    [
        (
            "InputImageSensitiveContentDetected.PrivacyInformation",
            "input_image_real_person",
            "真人",
        ),
        (
            "InputVideoSensitiveContentDetected.PrivacyInformation",
            "input_video_real_person",
            "参考视频",
        ),
        ("InputTextSensitiveContentDetected", "content_input_rejected", "提交的文字"),
        ("InputImageSensitiveContentDetected", "content_input_rejected", "提交的图片"),
        ("InputVideoSensitiveContentDetected", "content_input_rejected", "提交的视频"),
        ("OutputTextSensitiveContentDetected", "content_output_rejected", "生成的文字"),
        ("OutputImageSensitiveContentDetected", "content_output_rejected", "生成的图片"),
        ("OutputVideoSensitiveContentDetected", "content_output_rejected", "生成的视频"),
        (
            "OutputVideoSensitiveContentDetected.PrivacyInformation",
            "content_output_rejected",
            "生成的视频",
        ),
        ("content_policy_violation", "content_rejected", "未说明"),
        ("AccountOverdue", "account_payment_required", "余额"),
        ("insufficient_quota", "account_payment_required", "额度"),
        ("InvalidApiKey", "authentication_failed", "访问凭证"),
        ("AccessDenied", "permission_denied", "权限"),
        ("ModelNotFound", "model_not_found", "模型"),
        ("InvalidParameter", "invalid_parameters", "参数"),
        ("ContextLengthExceeded", "input_too_long", "长度限制"),
        ("InvalidParameter.ImageURL", "reference_unavailable", "参考素材"),
        ("RateLimitExceeded", "rate_limited", "并发上限"),
        ("ServiceUnavailable", "provider_unavailable", "稍后重试"),
    ],
)
def test_specific_provider_code_wins_over_generic_http_wrapper(provider_code, expected, phrase):
    error = ProviderCallError(
        "外层服务返回 HTTP 502",
        code="provider_model_unavailable",
        retryable=False,
        status_code=502,
        provider_code=provider_code,
        provider_request_id="req-123",
        provider_status_code=400,
    )
    result = describe_generation_error(error)
    assert result["code"] == expected
    assert phrase in result["message"]
    assert result["provider_code"] == provider_code
    assert result["provider_request_id"] == "req-123"
    assert result["provider_status_code"] == 400
    assert result["retryable"] is False  # Presentation does not change retry policy.
    assert "HTTP" not in result["message"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_parameters"),
        (401, "authentication_failed"),
        (402, "account_payment_required"),
        (403, "permission_denied"),
        (404, "model_not_found"),
        (408, "timeout"),
        (422, "invalid_parameters"),
        (413, "input_too_large"),
        (415, "unsupported_media_format"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (502, "provider_unavailable"),
        (503, "provider_unavailable"),
        (504, "timeout"),
    ],
)
def test_http_only_fallback_is_chinese(status, code):
    result = describe_generation_error(f"provider returned HTTP {status}")
    assert result["code"] == code
    assert result["provider_status_code"] == status
    assert "HTTP" not in result["message"]


@pytest.mark.parametrize(
    ("message", "phrase", "constraint"),
    [
        ("The duration must be between 5 and 15 seconds", "生成时长", "5 至 15秒"),
        ("The image size must be at least 3686400 pixels", "图片尺寸", "3686400像素"),
        ("The resolution must be one of ['2K', '3K', '4K']", "输出清晰度", "2K、3K、4K"),
        ("The aspect_ratio must be one of [16:9, 9:16]", "画面比例", "16:9、9:16"),
        ("Reference images: at most 9", "参考图片数量", "9张"),
        ("Reference videos: maximum 3", "参考视频数量", "3段"),
        ("The width must be at least 1024", "图片宽度", "1024像素"),
        ("Missing required parameter: prompt", "缺少提示词", "补充"),
    ],
)
def test_parameter_reason_translates_identified_fields_and_explicit_limits(
    message, phrase, constraint
):
    error = ProviderCallError(
        f"模型返回 HTTP 400: InvalidParameter / {message}",
        status_code=400,
        provider_code="InvalidParameter",
        provider_status_code=400,
    )
    result = describe_generation_error(error)
    assert result["code"] == "invalid_parameters"
    assert phrase in result["message"]
    assert constraint in result["message"]
    assert "InvalidParameter" not in result["message"]
    assert "must be" not in result["message"]


@pytest.mark.parametrize(
    "message",
    [
        "参考视频总时长不能超过 15 秒",
        "镜头裁剪范围超过源素材时长",
        "图片宽度至少为 1024 像素，请调整尺寸",
    ],
)
def test_specific_chinese_validation_is_preserved(message):
    assert describe_generation_error(ValueError(message), status_code=400)["message"] == message
    assert describe_generation_error(ValueError(message), status_code=502)["message"] == message


@pytest.mark.parametrize(
    ("error", "expected", "phrase"),
    [
        (TimeoutError(), "timeout", "超时"),
        ("ReadTimeout", "timeout", "超时"),
        ("ConnectError", "network_error", "连接"),
        ("RemoteProtocolError", "network_error", "连接"),
        ("provider unavailable", "provider_unavailable", "稍后重试"),
        ("FFmpeg failed under /tmp/private/clip", "render_failed", "视频合成失败"),
        (
            ProviderCallError("连续2次输出无效：title不能为空", code="provider_invalid_response"),
            "invalid_response",
            "有效的标题",
        ),
        (
            ProviderCallError("连续2次输出无效：body不能为空", code="provider_invalid_response"),
            "invalid_response",
            "文案正文",
        ),
        (
            ProviderCallError(
                "模型输出达到500 token上限并被截断", code="provider_invalid_response"
            ),
            "invalid_response",
            "被截断",
        ),
        (
            ProviderCallError("模型返回的标题与原文相同", code="provider_transform_no_effect"),
            "provider_transform_no_effect",
            "标题与原文相同",
        ),
        (
            ProviderCallError(
                "模型返回的正文与原内容相同，请调整指令后重试", code="provider_transform_no_effect"
            ),
            "provider_transform_no_effect",
            "正文与原内容相同",
        ),
    ],
)
def test_transport_and_invalid_result_errors(error, expected, phrase):
    result = describe_generation_error(error)
    assert result["code"] == expected
    assert phrase in result["message"]
    assert "title" not in result["message"]
    assert "body" not in result["message"]
    assert "token" not in result["message"]


def test_unknown_english_does_not_invent_an_explanation():
    result = describe_generation_error(
        "AI 服务返回：Totally new upstream failure", error_code="New.ProviderIssue"
    )
    assert result["code"] == "unknown_error"
    assert "未提供可识别" in result["message"]
    assert "New.ProviderIssue" in result["message"]
    assert "Totally" not in result["message"]


def test_seedance_output_copyright_restriction_is_actionable():
    result = describe_generation_error(
        "The request failed because the output video may be related to copyright restrictions. "
        "Request id: 02178946627572800000000000000000000ffffac14ddd0ed72e6"
    )
    assert result["code"] == "content_output_rejected"
    assert "版权" in result["message"]
    assert "不是地址过期" in result["message"]
    assert result["retryable"] is False


def test_hiagent_empty_reference_media_failure_is_actionable():
    result = describe_generation_error(
        "当前视频服务无法处理参考图片或参考视频，上游在接收素材时返回了空错误。"
        "请先清空参考图片和参考视频，只用提示词生成。"
    )
    assert result["code"] == "reference_unavailable"
    assert "参考" in result["message"]
    assert result["retryable"] is False


def test_empty_gateway_error_is_not_labeled_as_video_service():
    result = describe_generation_error("视频服务返回了空错误，请稍后重试")
    assert result["code"] == "invalid_response"
    assert "视频" not in result["message"]
    image_result = describe_generation_error("图片服务返回了空错误，请稍后重试")
    assert image_result["code"] == "invalid_response"
    assert "视频" not in image_result["message"]


def test_http_5xx_empty_gateway_error_is_provider_unavailable():
    result = describe_generation_error("视频服务返回了空错误（HTTP 500），请稍后重试")
    assert result["code"] == "provider_unavailable"
    assert "视频" not in result["message"]


def test_upload_empty_gateway_error_is_reference_unavailable():
    result = describe_generation_error("文件上传服务返回了空错误（HTTP 500），请稍后重试")
    assert result["code"] == "reference_unavailable"
    assert "参考" in result["message"]


def test_upload_http_413_is_input_too_large():
    result = describe_generation_error("文件上传服务返回了无法解析的错误（HTTP 413），请稍后重试")
    assert result["code"] == "input_too_large"
    assert "体积" in result["message"]
    assert "HTTP" not in result["message"]


@pytest.mark.parametrize(
    ("provider_code", "request_id", "has_identifier"),
    [
        (None, None, False),
        ("Bad code <html>", "request...", False),
        (None, "r" * 300, False),
        ("New.ProviderIssue", None, True),
        (None, "request-123", True),
    ],
)
def test_unknown_failure_only_mentions_error_identifier_when_one_is_available(
    provider_code, request_id, has_identifier
):
    result = describe_generation_error(
        ProviderCallError(
            "Unknown upstream problem", provider_code=provider_code, provider_request_id=request_id
        )
    )
    assert result["code"] == "unknown_error"
    if has_identifier:
        assert "凭错误编号排查" in result["message"]
    else:
        assert "查看服务日志排查" in result["message"]
        assert "错误编号" not in result["message"]


def test_chinese_wrappers_do_not_expose_unknown_english_or_internal_response_details():
    for message in (
        "AI生成失败：New upstream error happened unexpectedly",
        "ProviderCallError: 模型返回 HTTP 400: Unknown / Something really bad happened",
        "返回错误：{'source_ids': ['private-id'], 'attempt': 2}",
    ):
        result = describe_generation_error(message)
        assert "upstream" not in result["message"]
        assert "Something" not in result["message"]
        assert "source_ids" not in result["message"]
        assert "ProviderCallError" not in result["message"]


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        ("bearer-secret", "参数错误，Authorization: Bearer bearer-secret"),
        ("abc-secret", '参数错误 api_key="abc-secret"'),
        ("abc-secret", "参数错误 API key provided: abc-secret"),
        ("abc-secret", "参数错误 access_key_secret=abc-secret"),
        ("abcdefSECRET", "参数错误 sk-abcdefSECRET"),
        ("signed-token", "参数错误 https://host/image.png?Signature=signed-token"),
        ("AAAASECRET", "参数错误 data:image/png;base64,AAAASECRET"),
        ("raw-secret", "参数错误 data:text/plain,raw-secret"),
        ("hidden-file", '参数错误\nTraceback (most recent call last):\nFile "hidden-file"'),
        ("hidden-token", "<html><body>hidden-token</body></html>"),
        ("hidden-token", "<h1>hidden-token</h1>"),
        ("hidden-file", r"参数错误 C:\private\hidden-file.py"),
    ],
)
def test_credentials_links_payloads_html_and_stack_traces_are_removed(secret, message):
    safe = safe_generation_error_text(message)
    result = describe_generation_error(message, status_code=400)
    assert secret not in safe
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert "https://" not in safe
    assert "data:image" not in safe
    assert "Traceback" not in safe


def test_metadata_identifiers_are_whitelisted_and_never_truncated():
    error = ProviderCallError(
        "unknown", provider_code="Code <script>secret</script>", provider_request_id="r" * 300
    )
    result = describe_generation_error(error)
    assert "provider_code" not in result
    assert "provider_request_id" not in result
    assert "secret" not in json.dumps(result)
    for identifier in ("sk-secrettoken", "Bearer:private", "https://signed-url", "api_key:private"):
        assert "provider_request_id" not in describe_generation_error(
            ProviderCallError("unknown", provider_request_id=identifier)
        )
    assert (
        describe_generation_error("unknown Request id: provider-request-123")["provider_request_id"]
        == "provider-request-123"
    )
    for identifier in ("x" * 300, "sk-secrettoken", "short..."):
        assert "provider_request_id" not in describe_generation_error(
            f"unknown RequestId: {identifier}"
        )


def test_provider_json_metadata_extraction_is_limited_to_safe_fields():
    response = httpx.Response(
        402,
        json={
            "error": {
                "code": "AccountOverdue",
                "message": "余额不足 api_key=private-key",
                "request_id": "provider-req",
            },
            "headers": {"Authorization": "secret"},
        },
    )
    result = safe_provider_error_metadata(response)
    assert result["provider_code"] == "AccountOverdue"
    assert result["provider_request_id"] == "provider-req"
    assert result["provider_status_code"] == 402
    assert "余额不足" in result["message"]
    assert "private-key" not in json.dumps(result)
    assert "Authorization" not in json.dumps(result)
    volc = httpx.Response(
        403,
        json={
            "ResponseMetadata": {
                "RequestId": "request-2",
                "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
            }
        },
    )
    assert safe_provider_error_metadata(volc)["provider_request_id"] == "request-2"
    assert safe_provider_error_metadata(volc)["provider_code"] == "AccessDenied"


def test_non_json_html_is_not_copied_and_header_request_id_remains_safe():
    response = httpx.Response(
        502, text="<html><pre>secret-stack</pre></html>", headers={"x-request-id": "req-safe"}
    )
    assert safe_provider_error_metadata(response) == {
        "provider_status_code": 502,
        "provider_request_id": "req-safe",
    }


def _provider(model="primary"):
    return OpenAICompatibleTextProvider(
        provider_id="test-provider",
        base_url="https://test.invalid/v1",
        api_key="private-key",
        model=model,
        timeout_seconds=5,
        max_output_tokens=1000,
        temperature=0.1,
        max_attempts=2,
    )


def test_text_adapter_preserves_reason_metadata_and_original_auth_retry_policy(monkeypatch):
    real_client = httpx.Client
    posts = []

    def reject(request):
        posts.append(request)
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "InvalidApiKey",
                    "message": "Incorrect API key provided: private-key",
                    "request_id": "auth-req",
                }
            },
        )

    monkeypatch.setattr(
        llm_adapters.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(reject)),
    )
    with pytest.raises(ProviderCallError) as captured:
        _provider()._request_json("copies", {}, lambda value: value)
    error = captured.value
    assert len(posts) == 1
    assert posts[0].headers["Authorization"] == "Bearer private-key"
    assert error.code == "provider_rejected"
    assert error.retryable is False
    assert error.status_code == 401
    assert error.provider_status_code == 401
    assert error.provider_code == "InvalidApiKey"
    assert error.provider_request_id == "auth-req"
    assert "private-key" not in str(error)
    assert describe_generation_error(error)["code"] == "authentication_failed"


def test_text_failover_preserves_final_error_metadata_without_changing_attempts(monkeypatch):
    real_client = httpx.Client
    posts = []

    def reject(request):
        posts.append(json.loads(request.content)["model"])
        if len(posts) == 1:
            return httpx.Response(
                503, json={"error": {"code": "ServiceUnavailable", "message": "later"}}
            )
        return httpx.Response(
            402,
            json={
                "error": {
                    "code": "AccountOverdue",
                    "message": "Balance is insufficient",
                    "request_id": "last-req",
                }
            },
        )

    monkeypatch.setattr(
        llm_adapters.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(reject)),
    )
    provider = FailoverTextProvider(_provider("primary"), _provider("fallback"))
    with pytest.raises(ProviderCallError) as captured:
        provider._invoke("_request_json", "copies", {}, lambda value: value)
    assert posts == ["primary", "fallback"]
    assert captured.value.code == "provider_rejected"
    assert captured.value.retryable is False
    assert captured.value.provider_request_id == "last-req"
    assert captured.value.provider_status_code == 402
    assert describe_generation_error(captured.value)["code"] == "account_payment_required"


def test_media_privacy_error_preserves_legacy_status_and_true_provider_status():
    request = httpx.Request("POST", "https://test.invalid")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "code": "InputImageSensitiveContentDetected.PrivacyInformation",
                "message": "content[3] may contain real person",
            }
        },
        headers={"x-tt-logid": "media-req"},
    )
    error = media_adapters._safe_http_error(
        "test-provider", httpx.HTTPStatusError("bad", request=request, response=response)
    )
    assert error.status_code == 422
    assert error.code == "input_image_privacy_information"
    assert error.retryable is False
    assert error.provider_status_code == 400
    assert error.provider_request_id == "media-req"
    assert "content[3] may contain real person" in str(error)
    assert describe_generation_error(error)["provider_status_code"] == 400


def test_explicit_retry_override_and_legacy_exception_constructor_are_compatible():
    legacy = ProviderCallError("old message", code="old_code", retryable=True, status_code=503)
    assert legacy.provider_code is None
    assert legacy.provider_request_id is None
    assert describe_generation_error(legacy)["retryable"] is True
    assert describe_generation_error(legacy, retryable=False)["retryable"] is False
