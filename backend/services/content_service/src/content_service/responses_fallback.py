"""Resend an OpenAI chat-completions request through the Responses API.

Some relays keep /responses working while /chat/completions hangs (a
Cloudflare 524 after ~2 minutes) or returns 5xx. Callers try chat first and
use this on timeouts and server errors. After a successful fallback the same
endpoint goes straight to /responses for a while.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

import httpx

PREFER_SECONDS = 600
_preferred: dict[str, float] = {}
_lock = Lock()


def responses_endpoint(chat_endpoint: str) -> str:
    base = chat_endpoint.rstrip("/").removesuffix("/chat/completions")
    return base + "/responses"


def prefer_responses(chat_endpoint: str) -> bool:
    with _lock:
        since = _preferred.get(chat_endpoint)
        return since is not None and time.monotonic() - since < PREFER_SECONDS


def remember(chat_endpoint: str) -> None:
    with _lock:
        _preferred[chat_endpoint] = time.monotonic()


def should_fall_back(status_code: int | None) -> bool:
    """Timeouts/transport errors (None), gateway errors and 5xx; never auth or bad requests."""
    return status_code is None or status_code >= 500


def _content(role: str, value: Any) -> list[dict[str, Any]]:
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(value, str):
        return [{"type": text_type, "text": value}]
    parts = []
    for part in value or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": text_type, "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append({"type": "input_image", "image_url": url})
    return parts


def to_responses_payload(chat_payload: dict[str, Any]) -> dict[str, Any]:
    instructions, items = [], []
    for message in chat_payload.get("messages", []):
        role = message.get("role")
        if role in {"system", "developer"}:
            content = message.get("content")
            instructions.append(content if isinstance(content, str) else
                                "\n".join(part.get("text", "") for part in content or [] if isinstance(part, dict)))
        elif role in {"user", "assistant"}:
            items.append({"role": role, "content": _content(role, message.get("content"))})
    payload: dict[str, Any] = {"model": chat_payload["model"], "input": items}
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if chat_payload.get("max_tokens"):
        payload["max_output_tokens"] = chat_payload["max_tokens"]
    return payload


def as_chat_completion(result: dict[str, Any]) -> dict[str, Any]:
    """Shape a Responses result like a chat completion so existing parsing is reused."""
    text = ""
    for item in result.get("output", []) if isinstance(result, dict) else []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text += part.get("text") or ""
    incomplete = (result.get("incomplete_details") or {}).get("reason") if isinstance(result, dict) else None
    usage = result.get("usage") or {}
    return {
        "choices": [{
            "message": {"role": "assistant", "content": text},
            "finish_reason": "length" if incomplete == "max_output_tokens" else
            "content_filter" if incomplete == "content_filter" else "stop",
        }],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0)},
    }


def post(client: httpx.Client, chat_endpoint: str, headers: dict[str, str], chat_payload: dict[str, Any]) -> dict[str, Any]:
    """POST to /responses; raises httpx errors like a normal request."""
    response = client.post(responses_endpoint(chat_endpoint), headers=headers, json=to_responses_payload(chat_payload))
    response.raise_for_status()
    remember(chat_endpoint)
    return as_chat_completion(response.json())
