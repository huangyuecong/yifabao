"""Android connection discovery and verified draft submission dispatch."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypedDict

from .automation_browser import BrowserPublishError
from .automation_platforms import catalog_document, get_platform

CONFIG_LIMIT = 256_000
PROBE_TIMEOUT_SECONDS = 8
ADAPTER_MISSING = "安卓 App 发布适配器尚未完成实际验证，当前仅支持配置与设备检测。"


class PublishedReceipt(TypedDict):
    """Future success requires fresh platform evidence, never merely a completed tap."""

    status: str
    external_id: str
    account_label: str


class AndroidPublisher(Protocol):
    """Contract for future code-registered adapters, not configurable plug-ins."""

    def execute(
        self,
        platform: str,
        operation: str,
        payload: dict,
        connection: dict,
        *,
        device_id: str,
        before_submit: Callable[[], Any],
    ) -> PublishedReceipt: ...


def _blocked(code: str, message: str) -> BrowserPublishError:
    return BrowserPublishError(code, message)


def _config_path() -> Path:
    data_dir = Path(os.getenv("CONTENT_DATA_DIR", ".data/content")).resolve()
    private = (data_dir / "private").resolve()
    path = Path(
        os.getenv("CONTENT_PUBLISH_ANDROID_CONFIG", str(private / "publishing-android.json"))
    ).resolve()
    if not private.is_relative_to(data_dir) or not path.is_relative_to(private):
        raise _blocked("android_config_path_invalid", "安卓配置必须位于数据目录的 private 子目录。")
    return path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration field")
        result[key] = value
    return result


def _serial(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("device serial")
    emulator = re.fullmatch(r"emulator-([1-9][0-9]{0,4})", value)
    if emulator:
        if not 1 <= int(emulator[1]) <= 65535:
            raise ValueError("emulator port")
        return value
    network = re.fullmatch(r"(localhost|127(?:\.[0-9]{1,3}){3}|\[::1\]):([1-9][0-9]{0,4})", value)
    if not network or not 1 <= int(network[2]) <= 65535:
        raise ValueError("local device endpoint")
    host = network[1].strip("[]")
    if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
        raise ValueError("nonlocal device")
    # Treat the usual localhost spelling as the same endpoint for duplicate checks.
    return f"127.0.0.1:{network[2]}" if host == "localhost" else value


def _adb_path(value: Any) -> Path | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(char) < 32 for char in value)
        or value.startswith(("\\\\", "//"))
    ):
        raise ValueError("adb path")
    path = Path(value)
    if not path.is_absolute() or path.name.lower() not in {"adb", "adb.exe"}:
        raise ValueError("absolute adb executable")
    resolved = path.resolve()
    if str(resolved).startswith(("\\\\", "//")) or resolved.name.lower() not in {"adb", "adb.exe"}:
        raise ValueError("local adb executable")
    return resolved


def _load_config() -> dict:
    try:
        path = _config_path()
        if not path.is_file():
            raise _blocked("android_config_missing", "尚未配置本机安卓设备。")
        if path.stat().st_size > CONFIG_LIMIT:
            raise ValueError("large configuration")
        config = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
        if (
            not isinstance(config, dict)
            or type(config.get("version")) is not int
            or config["version"] != 1
            or set(config) - {"version", "adb_path", "adb_server_port", "ssh_tunnel", "devices"}
        ):
            raise ValueError("configuration schema")
        _adb_path(config.get("adb_path"))
        from .android_connection import validate_connection

        validate_connection(config)
        devices = config.get("devices")
        if not isinstance(devices, dict) or len(devices) > 30:
            raise ValueError("devices")
        serials = set()
        for device_id, device in devices.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id):
                raise ValueError("device identifier")
            if not isinstance(device, dict) or set(device) != {"label", "serial", "platforms"}:
                raise ValueError("device schema")
            label = device["label"]
            if (
                not isinstance(label, str)
                or not 1 <= len(label.strip()) <= 80
                or any(ord(char) < 32 or ord(char) == 127 for char in label)
            ):
                raise ValueError("device label")
            serial = _serial(device["serial"])
            if serial in serials:
                raise ValueError("duplicate device serial")
            serials.add(serial)
            platforms = device["platforms"]
            if not isinstance(platforms, list) or not platforms or len(platforms) > 30:
                raise ValueError("device platforms")
            if not all(isinstance(platform, str) for platform in platforms) or len(
                platforms
            ) != len(set(platforms)):
                raise ValueError("duplicate or invalid platform")
            # Existing server inventories can retain installed promotion Apps.
            # They are no longer selectable execution targets, but must not
            # invalidate the same device's supported Xianyu configuration.
            for platform in platforms:
                get_platform(platform)
        if config.get("ssh_tunnel"):
            expected = f"127.0.0.1:{config['ssh_tunnel']['local_port']}"
            if len(devices) != 1 or any(
                _serial(device["serial"]) != expected for device in devices.values()
            ):
                raise ValueError("SSH tunnel device mismatch")
        return config
    except BrowserPublishError:
        raise
    except (ValueError, TypeError, OSError, RecursionError):
        raise _blocked(
            "android_config_invalid", "安卓设备配置格式无效，请检查版本、ADB 路径、设备标识和平台。"
        ) from None


def _status(config: dict | None, missing: list[str]) -> dict:
    devices = [
        {
            "device_id": device_id,
            "label": device["label"],
            "configured": True,
            "state": "not_checked",
            "platforms": [
                platform
                for platform in device["platforms"]
                if "android" in get_platform(platform).execution_modes
            ],
        }
        for device_id, device in (config or {}).get("devices", {}).items()
    ]
    adb = _adb_path(config.get("adb_path")) if config else None
    adb_available = adb is not None and adb.is_file()
    reasons = list(missing)
    if not adb_available:
        reasons.append("尚未配置可用的本机 ADB 可执行文件。")
    if not devices:
        reasons.append("尚未配置安卓设备。")
    reasons.append(ADAPTER_MISSING)
    platforms = {}
    for spec in catalog_document():
        if "android" not in spec["execution_modes"]:
            continue
        platform_missing = list(reasons)
        if not any(spec["id"] in device["platforms"] for device in devices):
            platform_missing.append("该平台尚未绑定安卓设备。")
        platforms[spec["id"]] = {
            "configured": False,
            "ready": False,
            "missing": list(dict.fromkeys(platform_missing)),
            "accounts": [],
        }
    return {
        "configured": bool(devices),
        "available": adb_available,
        "ready": False,
        "adb_configured": adb_available,
        "missing": list(dict.fromkeys(reasons)),
        "devices": devices,
        "platforms": platforms,
    }


def _configured_status() -> tuple[dict | None, dict]:
    try:
        config = _load_config()
    except BrowserPublishError as error:
        return None, _status(None, [error.message])
    return config, _status(config, [])


def status(owner=None) -> dict:
    """Read configuration only; available means ADB exists, never that an App is ready."""
    config, document = _configured_status()
    if config:
        from .android_control import enrich_status

        return enrich_status(config, document, owner)
    return document


def _device_states(output: str) -> dict[str, str]:
    if not isinstance(output, str) or len(output) > CONFIG_LIMIT:
        raise ValueError("invalid adb output")
    lines = output.splitlines()
    header = next(
        (index for index, line in enumerate(lines) if line.strip() == "List of devices attached"),
        None,
    )
    if header is None or len(lines) > 1000:
        raise ValueError("invalid adb listing")
    result = {}
    for line in lines[header + 1 :]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            serial = _serial(parts[0])
        except ValueError:
            continue
        state = (
            parts[1]
            if parts[1]
            in {"device", "offline", "unauthorized", "bootloader", "recovery", "sideload"}
            else "unknown"
        )
        result[serial] = "ambiguous" if serial in result else state
    return result


def inspect_devices(owner=None) -> dict:
    """Explicit, read-only local discovery; never connects, installs, launches or taps."""
    config, document = _configured_status()
    if config is None or not document["adb_configured"]:
        return document
    if config.get("ssh_tunnel") or config.get("adb_server_port"):
        from .android_control import inspect_configured_devices

        return inspect_configured_devices(config, document, owner)
    adb = _adb_path(config.get("adb_path"))
    environment = os.environ.copy()
    for name in (
        "ADB_SERVER_SOCKET",
        "ANDROID_ADB_SERVER_ADDRESS",
        "ANDROID_ADB_SERVER_PORT",
        "ADB_SERVER_PORT",
        "ANDROID_SERIAL",
        "ADB_TRACE",
    ):
        environment.pop(name, None)
    environment["ADB_SERVER_SOCKET"] = "tcp:127.0.0.1:5037"
    environment["ANDROID_ADB_SERVER_PORT"] = "5037"
    try:
        result = subprocess.run(
            [str(adb), "devices", "-l"],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise ValueError("adb failed")
        states = _device_states(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        document["missing"].append("ADB 设备检测失败，请检查本机 ADB 和模拟器状态后重试。")
        for device in document["devices"]:
            device.update(connected=False, state="probe_failed")
        return document
    for device in document["devices"]:
        state = states.get(_serial(config["devices"][device["device_id"]]["serial"]), "not_found")
        device.update(connected=state == "device", state=state)
    if not any(device["connected"] for device in document["devices"]):
        document["missing"].append("未检测到已连接并授权的已配置安卓设备。")
    return document


def preflight(config: dict) -> None:
    from .android_publishing import preflight as check

    check(config)


def execute(
    platform: str,
    operation: str,
    payload: dict,
    connection: dict | None = None,
    *,
    device_id: str,
    before_submit: Callable[[], Any],
) -> PublishedReceipt:
    from .android_publishing import execute as submit

    return submit(
        platform,
        operation,
        payload,
        connection or {},
        device_id=device_id,
        before_submit=before_submit,
    )
