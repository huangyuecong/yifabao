import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from enum import Enum
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
logger = logging.getLogger(__name__)


def _log_token(value: object, pattern: str, fallback: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else fallback


def _log_unhandled(request: Request, exc: Exception, service_name: str) -> None:
    """Log bounded diagnostics without exception messages, SQL, values, or request data."""
    if getattr(request.state, "_unhandled_error_logged", False):
        return
    request.state._unhandled_error_logged = True
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    exceptions: list[dict[str, object]] = []
    codes: set[int] = set()
    while pending and len(exceptions) < 6:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        frames: deque[str] = deque(maxlen=8)
        tb = current.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            filename = re.split(r"[/\\]", code.co_filename)[-1]
            filename = _log_token(filename, r"[A-Za-z0-9_.-]{1,96}", "unknown")
            function = _log_token(code.co_name, r"[A-Za-z0-9_.<>]{1,96}", "unknown")
            frames.append(f"{filename}:{function}:{tb.tb_lineno}")
            tb = tb.tb_next
        exceptions.append({
            "type": _log_token(type(current).__name__, r"[A-Za-z0-9_]{1,96}", "Exception"),
            "frames": list(frames),
        })
        # dmPython puts its numeric code in a string argument. Never render an
        # SQLAlchemy exception, which can include statements and bound values.
        for argument in current.args[:4]:
            if isinstance(argument, str):
                for value in re.findall(r"\[CODE:([+-]?[0-9]{1,8})\]", argument[:4096])[:4]:
                    codes.add(int(value))
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        for nested in (cause, getattr(current, "orig", None)):
            if isinstance(nested, BaseException) and id(nested) not in seen:
                pending.append(nested)
    route = getattr(request.scope.get("route"), "path", None)
    request_id, _ = _request_identifiers(request)
    diagnostic = {
        "event": "unhandled_request_error",
        "service": _log_token(service_name, r"[A-Za-z0-9_.-]{1,96}", "unknown"),
        "method": _log_token(request.method, r"[A-Z]{1,16}", "OTHER"),
        "route": _log_token(route, r"/[A-Za-z0-9_/{}.:~-]{0,255}", "<unregistered>"),
        "request_id": _log_token(request_id, r"[A-Za-z0-9_.:-]{1,96}", "<invalid>"),
        "exceptions": exceptions,
        "dm_codes": sorted(codes)[:16],
    }
    logger.error("%s", json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":")))


def _json_safe(value: object) -> object:
    """Convert arbitrary exception payloads into JSON-safe values.

    Pydantic's validation context may contain a ValueError (under ``ctx.error``),
    and exception details can contain other non-serializable objects.  Keep
    useful scalar values while representing exception objects by their message.
    """
    if isinstance(value, BaseException):
        return str(value) or value.__class__.__name__
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    try:
        return jsonable_encoder(value)
    except Exception:
        return str(value)


def _request_identifiers(request: Request) -> tuple[str, str]:
    request_id = request_id_var.get() or request.headers.get("X-Request-Id") or str(uuid4())
    trace_id = trace_id_var.get() or request.headers.get("X-Trace-Id") or request_id
    return request_id, trace_id


def _title_for_status(status: int) -> str:
    return {
        400: "请求无效",
        401: "身份验证失败",
        403: "无权限",
        404: "资源不存在",
        405: "请求方法不允许",
        409: "业务状态冲突",
        422: "请求参数校验失败",
    }.get(status, "服务器内部错误" if status >= 500 else "请求处理失败")


def _problem(
    request: Request,
    *,
    status: int,
    title: str,
    detail: object,
    type_: str = "about:blank",
    errors: object = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id, trace_id = _request_identifiers(request)
    content = {
        "type": type_,
        "title": title,
        "status": status,
        "detail": _json_safe(detail),
        "instance": str(request.url.path),
        "request_id": request_id,
        "trace_id": trace_id,
    }
    if errors is not None:
        content["errors"] = _json_safe(errors)
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content=content,
        headers=dict(headers or {}),
    )


def install_request_context(app: FastAPI, service_name: str) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-Id") or str(uuid4())
        trace_id = request.headers.get("X-Trace-Id") or request_id
        request_id_var.set(request_id)
        trace_id_var.set(trace_id)
        try:
            response = await call_next(request)
        except Exception as exc:
            # ExceptionMiddleware re-raises unhandled exceptions after its
            # handler runs.  Catch at this outer boundary as well so a
            # TestClient/proxy never receives a plain-text 500 or a traceback.
            _log_unhandled(request, exc, service_name)
            response = _problem(
                request,
                status=500,
                type_="urn:yifabao:error:internal",
                title="服务器内部错误",
                detail="服务器暂时无法处理请求",
            )
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Agent-Service"] = service_name
        return response

    @app.exception_handler(HTTPException)
    async def http_problem(request: Request, exc: HTTPException) -> JSONResponse:
        return _problem(
            request,
            status=exc.status_code,
            title=_title_for_status(exc.status_code),
            detail=exc.detail,
            headers=exc.headers,
        )

    # Starlette raises this type for framework-generated 404/405 responses.
    # Registering it separately keeps those responses in the same contract.
    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_problem(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(
            request,
            status=exc.status_code,
            title=_title_for_status(exc.status_code),
            detail=exc.detail,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, exc: RequestValidationError) -> JSONResponse:
        try:
            errors: object = exc.errors()
        except Exception:
            errors = [{"msg": "请求字段不符合接口约束"}]
        return _problem(
            request,
            status=422,
            type_="urn:yifabao:error:validation",
            title="请求参数校验失败",
            detail="请求字段不符合接口约束",
            errors=errors,
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_problem(request: Request, exc: ValidationError) -> JSONResponse:
        try:
            errors: object = exc.errors()
        except Exception:
            errors = [{"msg": "请求字段不符合接口约束"}]
        return _problem(
            request,
            status=422,
            type_="urn:yifabao:error:validation",
            title="请求参数校验失败",
            detail="请求字段不符合接口约束",
            errors=errors,
        )

    @app.exception_handler(Exception)
    async def unhandled_problem(request: Request, _exc: Exception) -> JSONResponse:
        # Never expose stack traces, filesystem paths, or provider credentials.
        _log_unhandled(request, _exc, service_name)
        return _problem(
            request,
            status=500,
            type_="urn:yifabao:error:internal",
            title="服务器内部错误",
            detail="服务器暂时无法处理请求",
        )
