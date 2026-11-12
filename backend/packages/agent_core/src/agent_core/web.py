import json
import os
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_NAIVE_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$")


def _normalize_system_datetime_strings(
    value: Any,
    *,
    excluded_fields: frozenset[str],
) -> Any:
    """Attach the UTC offset to legacy naive system timestamps in JSON data.

    Pydantic correctly serializes aware datetimes, but a number of legacy API
    handlers return dictionaries that already contain ``datetime.isoformat()``
    strings.  SQLite also returns existing timezone-naive DATETIME rows.  The
    field-name guard keeps this compatibility normalization away from ordinary
    user-authored strings such as a meeting's local ``held_at`` value.
    """

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if (
                key.endswith("_at")
                and key not in excluded_fields
                and isinstance(item, str)
                and _NAIVE_ISO_DATETIME.fullmatch(item)
            ):
                normalized[key] = f"{item.replace(' ', 'T')}+00:00"
            else:
                normalized[key] = _normalize_system_datetime_strings(
                    item,
                    excluded_fields=excluded_fields,
                )
        return normalized
    if isinstance(value, list):
        return [
            _normalize_system_datetime_strings(item, excluded_fields=excluded_fields)
            for item in value
        ]
    return value


def _is_json_content_type(headers: list[tuple[bytes, bytes]]) -> bool:
    for name, value in headers:
        if name.lower() != b"content-type":
            continue
        media_type = value.decode("latin-1").split(";", 1)[0].strip().lower()
        return media_type == "application/json" or media_type.endswith("+json")
    return False


def _replace_content_length(
    headers: list[tuple[bytes, bytes]],
    body: bytes,
) -> list[tuple[bytes, bytes]]:
    updated = [(name, value) for name, value in headers if name.lower() != b"content-length"]
    updated.append((b"content-length", str(len(body)).encode("ascii")))
    return updated


class UTCSystemDatetimeResponseMiddleware:
    """Normalize system-generated ``*_at`` JSON fields to explicit UTC."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        excluded_fields: Collection[str] = (),
    ) -> None:
        self.app = app
        self.excluded_fields = frozenset(excluded_fields)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_start: Message | None = None
        body_parts: list[bytes] = []
        normalize_response = False

        async def send_wrapper(message: Message) -> None:
            nonlocal normalize_response, response_start

            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                normalize_response = _is_json_content_type(headers)
                if normalize_response:
                    response_start = dict(message)
                    return
                await send(message)
                return

            if normalize_response and message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))
                if message.get("more_body", False):
                    return

                original_body = b"".join(body_parts)
                normalized_body = original_body
                try:
                    payload = json.loads(original_body)
                    normalized_payload = _normalize_system_datetime_strings(
                        payload,
                        excluded_fields=self.excluded_fields,
                    )
                    if normalized_payload != payload:
                        normalized_body = json.dumps(
                            normalized_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass

                assert response_start is not None
                if normalized_body != original_body:
                    response_start["headers"] = _replace_content_length(
                        list(response_start.get("headers", [])),
                        normalized_body,
                    )
                await send(response_start)
                await send(
                    {
                        "type": "http.response.body",
                        "body": normalized_body,
                        "more_body": False,
                    }
                )
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)


def html_file(path: Path) -> HTMLResponse:
    return HTMLResponse(path.read_text(encoding="utf-8"))


def install_cors(app: FastAPI, env_name: str) -> None:
    raw = os.getenv(env_name, "http://127.0.0.1:5173,http://localhost:5173")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-Trace-Id", "X-Agent-Service"],
    )


def install_utc_datetime_responses(
    app: FastAPI,
    *,
    excluded_fields: Collection[str] = (),
) -> None:
    """Ensure legacy naive system timestamps have an explicit UTC offset."""

    app.add_middleware(
        UTCSystemDatetimeResponseMiddleware,
        excluded_fields=excluded_fields,
    )
