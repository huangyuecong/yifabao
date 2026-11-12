"""Exercise quota checks and cleanup against a simulated Android filesystem."""

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from content_service import android_media as media
from content_service.android_control import read_state, save_state
from content_service.automation_browser import BrowserPublishError


class Device:
    def __init__(self):
        self.files = {}
        self.pushes = []
        self.deleted = []
        self.free = 10 * 1024**3

    def shell(self, args, timeout=20):
        output, code = "", 0
        if args[0] == "df":
            output = f"Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/fuse 52428800 1024 {self.free // 1024} 1% /sdcard"
        elif args[0] == "du":
            size = sum(len(value) for target, value in self.files.items() if target.startswith(args[-1] + "/"))
            output = f"{(size + 1023) // 1024}\t{args[-1]}"
        elif args[0] == "sha256sum":
            value = self.files.get(args[1])
            if value is None:
                code = 1
            else:
                output = hashlib.sha256(value).hexdigest() + "  " + args[1]
        elif args[:2] == ["content", "query"]:
            output = "Row: 0 _id=42"
        elif args[0] == "rm":
            self.deleted.append(args[-1])
            self.files.pop(args[-1], None)
        return SimpleNamespace(output=output, exit_code=code)

    def push(self, source, target):
        self.pushes.append(target)
        self.files[target] = Path(source).read_bytes()


@pytest.fixture
def staging(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    config = {"devices": {"main": {"serial": "emulator-5554"}}}
    state = {"apps": {}}
    path = tmp_path / "video.mp4"
    path.write_bytes(b"test media")
    return Device(), config, state, path


def stage(staging):
    device, config, state, path = staging
    return media.stage(device, config, "main", state, "douyin", [path])


def test_repeated_content_uses_one_file_and_checks_remote_digest(staging):
    first = stage(staging)
    second = stage(staging)
    device, config, state, _ = staging
    assert first == second and len(device.pushes) == 1
    assert len(state["media"]) == 1
    assert read_state(config, "main")["storage"]["cache_bytes"] > 0
    device.files[first[0]["target"]] = b"corrupted remote media"
    stage(staging)
    assert len(device.pushes) == 2


def test_repeated_images_in_batch_are_transferred_once(staging):
    device, config, state, path = staging
    images = media.stage(device, config, "main", state, "xianyu", [path, path])
    assert len(images) == 2 and images[0]["target"] == images[1]["target"]
    assert len(device.pushes) == 1


@pytest.mark.parametrize("limit", ["disk", "cache", "batch"])
def test_limits_stop_before_transferring_any_bytes(staging, monkeypatch, limit):
    device, _, _, _ = staging
    if limit == "disk":
        device.free = media.MIN_FREE
    elif limit == "cache":
        monkeypatch.setattr(media, "CACHE_LIMIT", 1)
    else:
        monkeypatch.setattr(media, "BATCH_LIMIT", 1)
    with pytest.raises(BrowserPublishError):
        stage(staging)
    assert not device.pushes


def test_cleanup_deletes_only_expired_managed_copies(staging):
    imported = stage(staging)
    device, config, state, _ = staging
    target = imported[0]["target"]
    state["media"][target]["last_used"] = time.time() - media.RETENTION_SECONDS - 1
    external = "/sdcard/DCIM/photo.jpg"
    unknown_file = media.ROOTS[0] + "/my-photo.jpg"
    device.files.update({external: b"original", unknown_file: b"manual"})
    result = media.cleanup(device, config, "main", state)
    assert result["removed_files"] == 1 and device.deleted == [target]
    assert external in device.files and unknown_file in device.files


@pytest.mark.parametrize("kind", ["draft", "prepared", "unknown", "pending"])
def test_live_and_uncertain_assets_never_expire(staging, kind):
    imported = stage(staging)
    device, config, state, _ = staging
    entry = state["media"][imported[0]["target"]]
    entry["last_used"] = 0
    if kind == "draft":
        state["session"] = {"expires_at": time.time() + 100, "draft": {"imported": imported}}
    elif kind == "prepared":
        state["prepared"] = {"expires_at": time.time() + 100, "imported": imported}
    else:
        entry[kind + "_runs"] = ["run"]
    assert media.cleanup(device, config, "main", state)["removed_files"] == 0
    assert not device.deleted


def test_path_tampering_stops_cleanup_before_delete(staging):
    device, config, state, _ = staging
    state["media"] = {"/sdcard/DCIM/original.jpg": {"last_used": 0}}
    with pytest.raises(BrowserPublishError):
        media.cleanup(device, config, "main", state)
    assert not device.deleted


@pytest.mark.parametrize("kind", ["session", "prepared"])
def test_expired_editor_session_does_not_pin_old_media(staging, kind):
    imported = stage(staging)
    device, config, state, _ = staging
    state["media"][imported[0]["target"]]["last_used"] = 0
    state[kind] = {"expires_at": time.time() - 1}
    if kind == "session":
        state[kind]["draft"] = {"imported": imported}
    else:
        state[kind]["imported"] = imported
    assert media.cleanup(device, config, "main", state)["removed_files"] == 1
    assert device.deleted == [imported[0]["target"]]


def test_success_releases_its_copy_but_keeps_other_unknown_task(staging):
    imported = stage(staging)
    device, config, state, _ = staging
    media.mark_submission(state, imported, "one", "unknown")
    media.mark_submission(state, imported, "two", "pending")
    media.release_published(device, config, "main", state, imported, "two")
    assert not device.deleted
    assert state["media"][imported[0]["target"]]["unknown_runs"] == ["one"]


def test_success_removes_copy_and_manifest(staging):
    imported = stage(staging)
    device, config, state, _ = staging
    media.mark_submission(state, imported, "one", "pending")
    media.release_published(device, config, "main", state, imported, "one")
    assert device.deleted == [imported[0]["target"]] and state["media"] == {}


def test_interrupted_upload_is_tracked_before_push(staging, monkeypatch):
    device, config, state, _ = staging
    def fail_push(*args):
        assert read_state(config, "main")["media"]
        raise OSError("transfer interrupted")
    monkeypatch.setattr(device, "push", fail_push)
    with pytest.raises(OSError):
        stage(staging)
    assert len(state["media"]) == 1


def test_pending_marker_survives_process_loss(staging):
    imported = stage(staging)
    device, config, state, _ = staging
    media.mark_submission(state, imported, "run", "pending")
    save_state(config, "main", state)
    restored = read_state(config, "main")
    restored["media"][imported[0]["target"]]["last_used"] = 0
    assert media.cleanup(device, config, "main", restored)["removed_files"] == 0
