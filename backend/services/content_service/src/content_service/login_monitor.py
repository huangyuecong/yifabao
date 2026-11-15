"""Watch platform logins so a lost login shows up before a publish fails.

Every status check the workbench makes (opening the account panel, 检测登录, scanning a
QR code) is remembered here per browser owner — ``local`` for the workbench's own
browser, the trial account's name on the online trial. A background thread re-checks
the platforms that were logged in, so an expired login is noticed on its own:
every ``REMOTE_SECONDS`` for the server browsers, every ``LOCAL_SECONDS`` for an open
local browser (each check briefly opens and closes an account tab there).

The publish endpoints refuse a platform whose latest check says it is signed out, so a
missing login no longer turns into a failed publish record.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import suppress

from . import remote_browser

logger = logging.getLogger(__name__)
REMOTE_SECONDS = 120
LOCAL_SECONDS = 600
# A "signed out" result older than this is not trusted to block a publish.
FRESH_SECONDS = 15 * 60
SIGNED_OUT = {"login_required", "not_opened"}

_lock = threading.Lock()
# owner -> platform -> {"state", "message", "account_label", "checked_at", "lost_at"}
_states: dict[str, dict[str, dict]] = {}
_thread: threading.Thread | None = None


def owner_key() -> str:
    return (remote_browser.owner() or "local") if remote_browser.enabled() else "local"


def remember(result: dict, owner: str | None = None) -> dict:
    """Store one platform's status check; returns the stored entry."""
    platform = result.get("platform")
    state = result.get("state")
    if not platform or state in {None, "unavailable"}:
        return {}
    key = owner or owner_key()
    with _lock:
        platforms = _states.setdefault(key, {})
        previous = platforms.get(platform, {})
        entry = {
            "platform": platform,
            "state": state,
            "message": result.get("message", ""),
            "account_label": result.get("account_label", previous.get("account_label", "") if state == "logged_in" else ""),
            "checked_at": time.time(),
            "lost_at": None,
        }
        if state == "logged_in":
            entry["lost_at"] = None
        elif previous.get("state") == "logged_in" or previous.get("lost_at"):
            # It was logged in before: the login expired or was signed out elsewhere.
            entry["lost_at"] = previous.get("lost_at") or time.time()
        platforms[platform] = entry
        return dict(entry)


def remember_all(document: dict, owner: str | None = None) -> dict:
    for item in document.get("items", []):
        remember(item, owner)
    return document


def forget(platform: str | None = None, owner: str | None = None) -> None:
    key = owner or owner_key()
    with _lock:
        if platform is None:
            _states.pop(key, None)
        else:
            _states.get(key, {}).pop(platform, None)


def snapshot(owner: str | None = None) -> list[dict]:
    key = owner or owner_key()
    now = time.time()
    with _lock:
        items = [dict(item) for item in _states.get(key, {}).values()]
    for item in items:
        item["age_seconds"] = round(now - item["checked_at"])
    return sorted(items, key=lambda item: item["platform"])


def signed_out(platforms: list[str], owner: str | None = None) -> list[str]:
    """Platforms whose latest (recent) check says nobody is logged in."""
    key = owner or owner_key()
    now = time.time()
    # A closed local browser still keeps its logins in its profile; a closed server
    # browser is incognito and has none.
    states = SIGNED_OUT if remote_browser.enabled() else {"login_required"}
    with _lock:
        known = _states.get(key, {})
        return [
            platform for platform in platforms
            if platform in known and known[platform]["state"] in states
            and now - known[platform]["checked_at"] < FRESH_SECONDS
        ]


# --------------------------------------------------------------------------- background checks

def _due(owner: str, interval: float) -> list[str]:
    now = time.time()
    with _lock:
        return [
            platform for platform, entry in _states.get(owner, {}).items()
            if entry["state"] == "logged_in" and now - entry["checked_at"] >= interval
        ]


def _check(owner: str, platforms: list[str]) -> None:
    from . import automation_browser_session as sessions

    token = remote_browser.task_owner.set(owner if owner != "local" else None)
    quiet = remote_browser.monitoring.set(True)  # a check does not keep an idle browser open
    try:
        remember_all(sessions.sessions(tuple(platforms)), owner)
    except Exception:  # never let the watcher die
        logger.info("login monitor check failed for %s", owner, exc_info=True)
    finally:
        remote_browser.monitoring.reset(quiet)
        remote_browser.task_owner.reset(token)


def run_once() -> None:
    if remote_browser.enabled():
        for owner in remote_browser.running_owners():
            due = _due(owner, REMOTE_SECONDS)
            if due:
                _check(owner, due)
        # Browsers that closed (idle) took their logins with them.
        running = set(remote_browser.running_owners())
        with _lock:
            for owner, platforms in _states.items():
                if owner not in running:
                    for entry in platforms.values():
                        if entry["state"] == "logged_in":
                            entry.update(state="not_opened", message="服务器浏览器已关闭，登录信息已清除，请重新扫码。",
                                         lost_at=time.time(), checked_at=time.time(), account_label="")
    else:
        due = _due("local", LOCAL_SECONDS)
        if due:
            _check("local", due)


def start() -> None:
    """Start the watcher once per process (called on app startup)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return

    def loop():
        while True:
            time.sleep(30)
            with suppress(Exception):
                run_once()

    _thread = threading.Thread(target=loop, name="login-monitor", daemon=True)
    _thread.start()
