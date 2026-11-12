"""Import/reopen real draft state with Android transport and UI replaced."""

import time
from types import SimpleNamespace

import pytest
from PIL import Image

from content_service import android_control as control
from content_service import android_publishing as publishing
from content_service.automation_browser import BrowserPublishError


@pytest.fixture
def draft_device(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / "automation-media" / "run"
    directory.mkdir(parents=True)
    path = directory / "video.mp4"
    path.write_bytes(b"local media")
    config = {"devices": {"main": {"serial": "emulator-5554", "platforms": ["douyin"]}}}
    payload = {"title": "Original", "description": "Product introduction", "video_path": str(path)}
    state = {
        "apps": {
            "douyin": {
                "owner": "alice",
                "state": "logged_in",
                "identity": "alice123",
                "account_id": "account",
                "version": "1",
            }
        },
        "session": {
            "id": "a" * 32,
            "owner": "alice",
            "platform": "douyin",
            "expires_at": time.time() + 90,
        },
    }
    control.save_state(config, "main", state)
    calls = {"account": [], "imports": [], "share": [], "fill": []}

    def shell(args, **kwargs):
        calls["share"].append(args)
        return SimpleNamespace(output="Starting activity", exit_code=0)

    device = SimpleNamespace(
        screenshot=lambda: Image.new("RGB", (720, 1280)),
        app_info=lambda _: {"versionName": "1"},
        app_current=lambda: {"package": control.PACKAGES["douyin"]},
        shell=shell,
    )
    monkeypatch.setattr(control, "connect", lambda *_: device)
    monkeypatch.setattr(publishing, "connect", lambda *_: device)
    monkeypatch.setattr(publishing, "check_account", lambda *args: calls["account"].append(args))

    def import_media(_device, platform, value, **kwargs):
        calls["imports"].append(dict(value))
        suffix = publishing.validated_media(platform, value)[0].suffix
        return [
            {
                "target": "/sdcard/Movies/AIWorkbench/" + "f" * 64 + suffix,
                "uri": "content://media/external/video/media/1",
                "sha256": "f" * 64,
            }
        ]

    monkeypatch.setattr(publishing, "import_media", import_media)
    monkeypatch.setattr(publishing, "fill_text", lambda *args: calls["fill"].append(args[-1]))
    return config, payload, device, calls


def open_draft(fixture, payload=None):
    config, original, _, _ = fixture
    return publishing.open_draft(
        config, "main", "douyin", "alice", "run", payload or original, "account"
    )


def test_changed_saved_draft_is_reimported_instead_of_returning_old_copy(draft_device):
    config, payload, _, calls = draft_device
    first = open_draft(draft_device)
    updated = {**payload, "title": "Updated title", "description": "Updated introduction"}

    result = open_draft(draft_device, updated)

    assert result["session_id"] == first["session_id"]
    assert result["draft"]["fields"]["title"] == "Updated title"
    assert calls["imports"] == [payload, updated]
    assert len(calls["account"]) == 2
    assert control.read_state(config, "main")["session"]["draft"][
        "digest"
    ] == publishing.payload_digest(updated)


def test_unchanged_draft_resumes_without_restarting_editor_or_reimporting(draft_device):
    first = open_draft(draft_device)
    second = open_draft(draft_device)

    assert second["draft"] == first["draft"]
    assert len(draft_device[3]["imports"]) == len(draft_device[3]["account"]) == 1


def test_updated_app_requires_fresh_draft_preparation(draft_device):
    config, _, device, calls = draft_device
    open_draft(draft_device)
    device.app_info = lambda _: {"versionName": "2"}

    open_draft(draft_device)

    assert len(calls["account"]) == len(calls["imports"]) == 2
    assert control.read_state(config, "main")["session"]["draft"]["version"] == "2"


@pytest.mark.parametrize("failure", ["exception", "exit_code", "error_output"])
def test_share_failure_keeps_imported_draft_and_usable_album_screen(draft_device, failure):
    config, _, device, calls = draft_device

    def failed_share(*args, **kwargs):
        if failure == "exception":
            raise OSError("share activity unavailable")
        return SimpleNamespace(
            output="Error: activity unavailable" if failure == "error_output" else "",
            exit_code=1 if failure == "exit_code" else 0,
        )

    device.shell = failed_share

    result = open_draft(draft_device)

    assert result["draft"]["media_count"] == 1
    assert "相册" in result["draft"]["note"]
    assert control.read_state(config, "main")["session"]["draft"]["run_id"] == "run"
    assert len(calls["account"]) == len(calls["imports"]) == 1
    assert not calls["fill"]


def test_account_mismatch_stops_before_media_import_but_keeps_login_screen(
    draft_device, monkeypatch
):
    config, _, _, calls = draft_device

    def wrong_account(*args):
        raise BrowserPublishError("android_account_changed", "Account changed")

    monkeypatch.setattr(publishing, "check_account", wrong_account)
    with pytest.raises(BrowserPublishError) as exc:
        open_draft(draft_device)

    assert exc.value.code == "android_account_changed"
    assert not calls["imports"] and not calls["share"]
    stored = control.read_state(config, "main")
    assert stored["session"]["id"] == "a" * 32
    assert not stored["session"].get("draft")


def test_reimport_failure_invalidates_old_draft_without_losing_session(draft_device, monkeypatch):
    config, payload, _, _ = draft_device
    open_draft(draft_device)

    def failed_import(*args, **kwargs):
        raise BrowserPublishError("android_media_transfer_failed", "Transfer failed")

    monkeypatch.setattr(publishing, "import_media", failed_import)
    with pytest.raises(BrowserPublishError):
        open_draft(draft_device, {**payload, "title": "Updated"})

    stored = control.read_state(config, "main")
    assert stored["session"]["id"] == "a" * 32
    assert "draft" not in stored["session"]


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [(".mp4", "video/mp4"), (".mov", "video/quicktime"), (".m4v", "video/x-m4v")],
)
def test_share_uses_imported_file_mime_type(draft_device, suffix, expected):
    _, payload, _, calls = draft_device
    from pathlib import Path

    path = Path(payload["video_path"]).with_suffix(suffix)
    path.write_bytes(b"video media")
    open_draft(draft_device, {**payload, "video_path": str(path)})

    command = calls["share"][0]
    assert command[command.index("-t") + 1] == expected
