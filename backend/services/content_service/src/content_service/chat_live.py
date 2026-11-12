"""In-process live preview of chat replies, streamed to the browser over SSE.

The database stays the source of truth: a reply is complete only after
ChatStore.finish has written it. Live state is a best-effort preview kept in
the worker's own process; clients that miss it keep polling the conversation.
"""

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from threading import Condition

KEEP_SECONDS = 120
MAX_FOLLOW_SECONDS = 1800


@dataclass
class _Reply:
    owner: str
    conversation_id: str
    events: list[tuple[str, dict]] = field(default_factory=list)
    done: bool = False
    finished_at: float = 0.0


_condition = Condition()
_replies: dict[str, _Reply] = {}


def enabled() -> bool:
    return os.getenv("CONTENT_CHAT_STREAMING", "true").strip().lower() not in {"0", "false", "no", "off"}


def _prune(now: float) -> None:
    for key in [key for key, reply in _replies.items() if reply.done and now - reply.finished_at > KEEP_SECONDS]:
        del _replies[key]


def start(reply_id: str, owner: str, conversation_id: str) -> None:
    """Open a fresh preview; a retry reuses the reply id and replaces the old one."""
    with _condition:
        _prune(time.monotonic())
        _replies[reply_id] = _Reply(owner, conversation_id)
        _condition.notify_all()


def _emit(reply_id: str, kind: str, data: dict) -> None:
    with _condition:
        reply = _replies.get(reply_id)
        if reply is None or reply.done:
            return
        reply.events.append((kind, data))
        _condition.notify_all()


def event(reply_id: str, kind: str, data: dict) -> None:
    """A structured event, e.g. the Agent's "plan" and "step" updates."""
    _emit(reply_id, kind, data)


def stage(reply_id: str, label: str) -> None:
    _emit(reply_id, "stage", {"label": label})


def delta(reply_id: str, text: str) -> None:
    if text:
        _emit(reply_id, "delta", {"text": text})


def reset(reply_id: str) -> None:
    """Discard streamed text, e.g. before falling back to a complete reply."""
    _emit(reply_id, "reset", {})


def end(reply_id: str) -> None:
    with _condition:
        reply = _replies.get(reply_id)
        if reply is None or reply.done:
            return
        reply.events.append(("end", {"live": True}))
        reply.done = True
        reply.finished_at = time.monotonic()
        _condition.notify_all()


def follow(
    reply_id: str, owner: str, conversation_id: str, *, heartbeat: float = 15
) -> Iterator[tuple[str, dict | None]]:
    """Yield events from the start of a reply, ("ping", None) while idle, and finally "end"."""
    cursor = 0
    tracked = None
    deadline = time.monotonic() + MAX_FOLLOW_SECONDS
    while True:
        with _condition:
            reply = _replies.get(reply_id)
            if reply is None or reply.owner != owner or reply.conversation_id != conversation_id:
                yield "end", {"live": False}
                return
            if reply is not tracked:
                # A retry replaced the preview: start again from its first event.
                tracked, cursor = reply, 0
                yield "reset", {}
            if cursor >= len(reply.events) and not reply.done:
                _condition.wait(heartbeat)
            batch = reply.events[cursor:]
            cursor += len(batch)
            finished = reply.done and cursor >= len(reply.events)
        for kind, data in batch:
            yield kind, data
            if kind == "end":
                return
        if finished:
            yield "end", {"live": True}
            return
        if not batch:
            if time.monotonic() > deadline:
                yield "end", {"live": False}
                return
            yield "ping", None
