"""Bounded, content-addressed Android staging; never delete source-library or App data.

All mutations run under android_control.device_lock. The manifest is written before
transferring a file so interrupted uploads remain tracked and can expire safely.
"""

from __future__ import annotations

import hashlib
import re
import time

from .android_control import fail, save_state

MIB = 1024 * 1024
CACHE_LIMIT = 2048 * MIB
MIN_FREE = 2048 * MIB
BATCH_LIMIT = 512 * MIB
RETENTION_SECONDS = 24 * 3600
ROOTS = ("/sdcard/Pictures/AIWorkbench", "/sdcard/Movies/AIWorkbench")
TARGET = re.compile(
    r"/sdcard/(?:Pictures|Movies)/AIWorkbench/[a-f0-9]{64}\.(?:jpg|jpeg|png|webp|mp4|mov|m4v)\Z"
)


def shell(device, args, timeout=20):
    result = device.shell(args, timeout=timeout)
    if result.exit_code != 0:
        fail("android_storage_unavailable", "无法读取安卓素材空间，请重新检测设备。")
    return result.output.strip()


def storage_snapshot(device, state):
    lines = shell(device, ["df", "-k", "/sdcard"]).splitlines()
    if len(lines) < 2:
        fail("android_storage_unavailable", "安卓磁盘信息无效。")
    try:
        total, used, free = (int(value) * 1024 for value in lines[-1].split()[1:4])
        if min(total, used, free) < 0 or total == 0:
            raise ValueError()
    except (ValueError, TypeError):
        fail("android_storage_unavailable", "安卓磁盘信息无效。")
    cache = 0
    for root in ROOTS:
        # Create only the application's own staging directories.
        shell(device, ["mkdir", "-p", root])
        value = shell(device, ["du", "-sk", root]).split()
        if not value or not value[0].isdigit():
            fail("android_storage_unavailable", "安卓素材空间信息无效。")
        cache += int(value[0]) * 1024
    protected = protected_targets(state)
    state["storage"] = {
        "total_bytes": total,
        "free_bytes": free,
        "cache_bytes": cache,
        "cache_limit_bytes": CACHE_LIMIT,
        "minimum_free_bytes": MIN_FREE,
        "batch_limit_bytes": BATCH_LIMIT,
        "retention_hours": RETENTION_SECONDS // 3600,
        "protected_files": len(protected),
        "checked_at": time.time(),
    }
    return state["storage"]


def protected_targets(state):
    targets = set()
    session = state.get("session", {})
    prepared = state.get("prepared", {})
    containers = []
    if session.get("expires_at", 0) > time.time():
        containers.append(session.get("draft", {}))
    if prepared.get("expires_at", 0) > time.time():
        containers.append(prepared)
    for container in containers:
        targets.update(
            item.get("target") for item in container.get("imported", []) if isinstance(item, dict)
        )
    for target, item in state.get("media", {}).items():
        if item.get("pending_runs") or item.get("unknown_runs"):
            targets.add(target)
    return targets


def _delete(device, target):
    if not isinstance(target, str) or not TARGET.fullmatch(target):
        fail("android_media_manifest_invalid", "素材记录路径无效，已停止清理。")
    # Do not trust a saved MediaStore URI: remove only the row for this exact path.
    android_path = target.replace("/sdcard/", "/storage/emulated/0/", 1)
    shell(
        device,
        [
            "content",
            "delete",
            "--uri",
            "content://media/external/file",
            "--where",
            f"_data='{android_path}'",
        ],
    )
    shell(device, ["rm", "-f", "--", target])


def cleanup(device, config, device_id, state):
    protected = protected_targets(state)
    removed = 0
    for target, item in list(state.get("media", {}).items()):
        if (
            target in protected
            or time.time() - item.get("last_used", time.time()) < RETENTION_SECONDS
        ):
            continue
        _delete(device, target)
        state["media"].pop(target)
        save_state(config, device_id, state)
        removed += 1
    storage_snapshot(device, state)
    save_state(config, device_id, state)
    return {"removed_files": removed, **state["storage"]}


def _remote_digest(device, target):
    result = device.shell(["sha256sum", target], timeout=40)
    if result.exit_code:
        return None
    value = result.output.split()
    return value[0] if value and re.fullmatch(r"[a-f0-9]{64}", value[0]) else None


def stage(device, config, device_id, state, platform, paths):
    """Plan/check the whole batch before pushing; return exact MediaStore URIs."""
    if sum(path.stat().st_size for path in paths) > BATCH_LIMIT:
        fail("android_media_batch_too_large", "本次素材超过 512MB，请压缩视频或减少图片。")
    planned = []
    for path in paths:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        root = ROOTS[0] if platform == "xianyu" else ROOTS[1]
        target = f"{root}/{digest}{path.suffix.lower()}"
        planned.append(
            {"path": path, "target": target, "sha256": digest, "size": path.stat().st_size}
        )
    # A duplicate must not be expired just before reusing it.
    registry = state.setdefault("media", {})
    for item in planned:
        if item["target"] in registry:
            registry[item["target"]]["last_used"] = time.time()
    cleanup(device, config, device_id, state)
    unique = {item["target"]: item for item in planned}
    for item in unique.values():
        item["reuse"] = _remote_digest(device, item["target"]) == item["sha256"]
    incoming = sum(item["size"] for item in unique.values() if not item["reuse"])
    snapshot = state["storage"]
    if snapshot["cache_bytes"] + incoming > CACHE_LIMIT:
        fail(
            "android_media_cache_full",
            "安卓临时素材已达到 2GB 上限。请先完成待发布任务，或清理过期素材。",
        )
    if snapshot["free_bytes"] - incoming < MIN_FREE:
        fail("android_disk_low", "服务器磁盘不足，需保留至少 2GB 空间；本次未传输素材。")
    imported = {}
    for target, item in unique.items():
        record = registry.setdefault(target, {})
        record.update(sha256=item["sha256"], size=item["size"], last_used=time.time())
        save_state(config, device_id, state)
        if not item["reuse"]:
            # Push streams the local file; it is not loaded in one RAM buffer.
            device.push(str(item["path"]), target)
            if _remote_digest(device, target) != item["sha256"]:
                fail("android_media_transfer_failed", "素材传输校验未通过，请重新导入。")
        shell(
            device,
            [
                "am",
                "broadcast",
                "-a",
                "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d",
                "file://" + target,
            ],
        )
        deadline = time.monotonic() + 12
        media_id = None
        while time.monotonic() < deadline:
            output = shell(
                device,
                [
                    "content",
                    "query",
                    "--uri",
                    "content://media/external/file",
                    "--projection",
                    "_id",
                    "--where",
                    "_data='" + target.replace("/sdcard/", "/storage/emulated/0/", 1) + "'",
                ],
            )
            matches = re.findall(r"\b_id=(\d+)", output)
            if len(matches) == 1:
                media_id = matches[0]
                break
            time.sleep(0.5)
        if not media_id:
            fail("android_media_scan_failed", "素材已传到安卓，但相册尚未识别，请稍后重试。")
        media_type = "images" if platform == "xianyu" else "video"
        imported[target] = {
            "uri": f"content://media/external/{media_type}/media/{media_id}",
            "name": item["path"].name,
            "target": target,
            "sha256": item["sha256"],
        }
    storage_snapshot(device, state)
    save_state(config, device_id, state)
    return [imported[item["target"]] for item in planned]


def mark_submission(state, imported, run_id, result):
    for item in imported:
        if not isinstance(item, dict):
            continue
        entry = state.get("media", {}).get(item.get("target"))
        if not entry:
            continue
        pending, unknown = set(entry.get("pending_runs", [])), set(entry.get("unknown_runs", []))
        pending.discard(run_id)
        if result == "pending":
            pending.add(run_id)
        if result == "unknown":
            unknown.add(run_id)
        entry.update(
            pending_runs=sorted(pending), unknown_runs=sorted(unknown), last_used=time.time()
        )


def release_published(device, config, device_id, state, imported, run_id):
    mark_submission(state, imported, run_id, "published")
    save_state(config, device_id, state)
    protected = protected_targets(state)
    for item in imported:
        target = item["target"]
        if target not in protected and target in state.get("media", {}):
            _delete(device, target)
            state["media"].pop(target)
            save_state(config, device_id, state)
    storage_snapshot(device, state)
    save_state(config, device_id, state)
