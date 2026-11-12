"""A dedicated loopback ADB connection, optionally carried by verified SSH."""

from __future__ import annotations

import atexit
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .automation_browser import BrowserPublishError

_owned: dict[tuple, subprocess.Popen] = {}
_clients: dict[tuple, object] = {}
_mutex = threading.RLock()


def validate_connection(config: dict) -> None:
    port = config.get("adb_server_port", 5037)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("ADB server port")
    tunnel = config.get("ssh_tunnel")
    if tunnel is None:
        return
    if not isinstance(tunnel, dict) or set(tunnel) != {
        "host",
        "user",
        "key_path",
        "local_port",
        "remote_port",
    }:
        raise ValueError("SSH tunnel schema")
    if not isinstance(tunnel["host"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", tunnel["host"]
    ):
        raise ValueError("SSH host")
    if not isinstance(tunnel["user"], str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_-]{0,63}", tunnel["user"]
    ):
        raise ValueError("SSH user")
    for field in ("local_port", "remote_port"):
        if type(tunnel[field]) is not int or not 1024 <= tunnel[field] <= 65535:
            raise ValueError("SSH port")
    if tunnel["local_port"] == port:
        raise ValueError("conflicting ports")
    key = tunnel["key_path"]
    if (
        not isinstance(key, str)
        or not Path(key).is_absolute()
        or key.startswith(("//", "\\\\"))
        or any(ord(c) < 32 for c in key)
    ):
        raise ValueError("SSH identity path")


def environment(config: dict) -> dict:
    env = os.environ.copy()
    for name in (
        "ADB_SERVER_SOCKET",
        "ANDROID_ADB_SERVER_ADDRESS",
        "ANDROID_ADB_SERVER_PORT",
        "ADB_SERVER_PORT",
        "ANDROID_SERIAL",
        "ADB_TRACE",
    ):
        env.pop(name, None)
    port = config.get("adb_server_port", 5037)
    env.update(ADB_SERVER_SOCKET=f"tcp:127.0.0.1:{port}", ANDROID_ADB_SERVER_PORT=str(port))
    return env


def adb(config: dict, args: list[str], *, timeout=20, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            [config["adb_path"], "-P", str(config.get("adb_server_port", 5037)), *args],
            input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=environment(config),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise OSError()
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise BrowserPublishError(
            "android_connection_failed", "安卓连接中断，请重新检测设备。"
        ) from None


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _devices_online(config: dict) -> bool:
    """Query the dedicated ADB server without spawning a process for every frame."""
    try:
        import adbutils

        client = adbutils.AdbClient(
            host="127.0.0.1", port=config.get("adb_server_port", 5037), socket_timeout=2
        )
        states = {item.serial: item.state for item in client.list()}
        return bool(config["devices"]) and all(
            states.get(device["serial"]) == "device" for device in config["devices"].values()
        )
    except Exception:
        return False


def ensure_connection(config: dict) -> None:
    validate_connection(config)
    with _mutex:
        tunnel = config.get("ssh_tunnel")
        if tunnel:
            if not Path(tunnel["key_path"]).is_file() or not shutil.which("ssh"):
                raise BrowserPublishError(
                    "android_ssh_missing", "服务器连接所需的 SSH 身份或程序不可用。"
                )
            identity = (tunnel["host"], tunnel["user"], tunnel["local_port"], tunnel["remote_port"])
            if not _listening(tunnel["local_port"]):
                old = _owned.get(identity)
                if old and old.poll() is None:
                    old.terminate()
                    old.wait(timeout=5)
                process = subprocess.Popen(
                    [
                        shutil.which("ssh"),
                        "-N",
                        "-T",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "StrictHostKeyChecking=yes",
                        "-o",
                        "ExitOnForwardFailure=yes",
                        "-o",
                        "ConnectTimeout=10",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=3",
                        "-i",
                        tunnel["key_path"],
                        "-L",
                        f"127.0.0.1:{tunnel['local_port']}:127.0.0.1:{tunnel['remote_port']}",
                        f"{tunnel['user']}@{tunnel['host']}",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                _owned[identity] = process
                deadline = time.monotonic() + 12
                while not _listening(tunnel["local_port"]):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        if process.poll() is None:
                            process.terminate()
                        raise BrowserPublishError(
                            "android_ssh_failed", "服务器连接失败，请检查网络和 SSH 身份。"
                        )
                    time.sleep(0.2)
        if _devices_online(config):
            return
        adb(config, ["start-server"], timeout=12)
        for device in config["devices"].values():
            if ":" in device["serial"]:
                adb(config, ["connect", device["serial"]], timeout=12)


def connect(config: dict, device_id: str):
    if device_id not in config["devices"]:
        raise BrowserPublishError("android_device_missing", "请选择已配置的安卓设备。")
    with _mutex:
        ensure_connection(config)
        key = (
            config["adb_path"],
            config.get("adb_server_port", 5037),
            config["devices"][device_id]["serial"],
            tuple(sorted((config.get("ssh_tunnel") or {}).items())),
        )
        try:
            import adbutils

            client = adbutils.AdbClient(
                host="127.0.0.1", port=config.get("adb_server_port", 5037), socket_timeout=5
            )
            device = client.device(config["devices"][device_id]["serial"])
            if device.get_state() != "device":
                raise OSError()
        except Exception:
            _clients.pop(key, None)
            raise BrowserPublishError(
                "android_device_offline", "安卓设备未连接或未授权，请重新检测。"
            ) from None
        if key in _clients:
            return _clients[key]
        try:
            import uiautomator2

            # Its RPC client restarts the accessibility service after a disconnect.
            # Recreating it on every screenshot repeats jar checks and atexit hooks.
            connected = uiautomator2.connect(device)
        except Exception:
            raise BrowserPublishError(
                "android_control_unavailable",
                "安卓已连接，但屏幕控制服务尚未就绪，请重新打开登录窗口。",
            ) from None
        if len(_clients) >= 32:
            _clients.pop(next(iter(_clients)))
        _clients[key] = connected
        return connected


@atexit.register
def close_owned_connections():
    for process in _owned.values():
        if process.poll() is None:
            process.terminate()
