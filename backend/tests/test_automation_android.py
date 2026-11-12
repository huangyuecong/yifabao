"""Android infrastructure tests use stub ADB output; no executable or device is run."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from content_service import automation_android as android
from content_service import automation_browser as browser


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTENT_PUBLISH_ANDROID_CONFIG", raising=False)
    private = tmp_path / "private"
    private.mkdir()
    adb = tmp_path / "private-adb-location" / "adb.exe"
    adb.parent.mkdir()
    adb.write_bytes(b"not an executable; subprocess must be stubbed")
    config = {
        "version": 1,
        "adb_path": str(adb),
        "devices": {
            "main": {
                "label": "本机模拟器",
                "serial": "emulator-5554",
                "platforms": ["xianyu", "douyin"],
            },
            "second": {
                "label": "第二模拟器",
                "serial": "127.0.0.1:62001",
                "platforms": ["xiaohongshu", "kuaishou"],
            },
        },
    }
    path = private / "publishing-android.json"

    def save():
        path.write_text(json.dumps(config), encoding="utf-8")

    save()
    # Even an accidental probe during a status test cannot touch an actual device.
    monkeypatch.setattr(
        android.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Unexpected device command"),
    )
    return config, path, save


def test_missing_configuration_is_truthful_and_does_not_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CONTENT_PUBLISH_ANDROID_CONFIG", raising=False)
    monkeypatch.setattr(
        android.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Unexpected device command"),
    )
    for result in (android.status(), android.inspect_devices()):
        assert not result["ready"] and not result["available"] and not result["configured"]
        assert not result["adb_configured"] and result["devices"] == []
        assert result["missing"]


def test_status_reads_config_without_claiming_connection_or_publishing(configured):
    config, _, _ = configured
    result = android.status()
    assert result["available"] and result["adb_configured"] and result["configured"]
    assert result["ready"] is False
    # Publishing currently runs in the browser only, so no platform is an Android
    # execution target; configured Apps stay in the file without being offered.
    assert result["platforms"] == {}
    assert result["devices"][0]["platforms"] == []
    for platform in result["platforms"].values():
        assert platform["configured"] is False and platform["ready"] is False
        assert platform["accounts"] == [] and platform["missing"]
    for device in result["devices"]:
        assert device["configured"] and device["state"] == "not_checked"
        assert "connected" not in device
    serialized = json.dumps(result)
    assert config["adb_path"] not in serialized and "private-adb-location" not in serialized
    assert "emulator-5554" not in serialized and "62001" not in serialized


def test_uninstalled_adb_never_probes_or_claims_available(configured):
    config, path, save = configured
    config["adb_path"] = str(path.parent / "not-installed" / "adb.exe")
    save()
    result = android.inspect_devices()
    assert result["configured"] and not result["available"] and not result["adb_configured"]
    assert not result["ready"] and result["missing"]


@pytest.mark.parametrize(
    "serial",
    [
        "192.168.1.2:5555",
        "example.com:5555",
        "127.0.0.1.evil.example:5555",
        "emulator-0",
        "emulator-65536",
        "emulator-05554",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.999:5555",
        "[2001:db8::1]:5555",
        "localhost:5555; shell tap 1 1",
        "-s emulator-5554",
        "physical-usb-serial",
        "emulator-5554\n",
    ],
)
def test_nonlocal_ambiguous_or_command_like_serials_are_rejected(configured, serial):
    config, _, save = configured
    config["devices"]["main"]["serial"] = serial
    save()
    result = android.inspect_devices()
    assert result["configured"] is False and result["devices"] == []


@pytest.mark.parametrize(
    "adb_path", ["adb", "./adb.exe", "cmd.exe", "C:/adb.exe & echo token", "//host/share/adb.exe"]
)
def test_executable_must_be_an_explicit_local_adb_path(configured, adb_path):
    config, _, save = configured
    config["adb_path"] = adb_path
    save()
    assert android.inspect_devices()["configured"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda config: config.update(version=True),
        lambda config: config.update(verified=True),
        lambda config: config.update(command=["shell", "input", "tap"]),
        lambda config: config["devices"]["main"].update(verified=True),
        lambda config: config["devices"]["main"].update(platforms=["unregistered-app"]),
        lambda config: config["devices"]["main"].update(platforms=["douyin", "douyin"]),
        lambda config: config["devices"]["main"].update(label="unsafe\nlabel"),
        lambda config: config["devices"].update({"invalid/id": config["devices"]["main"]}),
        lambda config: config["devices"]["second"].update(serial="emulator-5554"),
    ],
)
def test_invalid_config_cannot_enable_adapters_or_commands(configured, change):
    config, _, save = configured
    change(config)
    save()
    assert android.inspect_devices()["configured"] is False


def test_config_override_cannot_leave_private_directory(configured, tmp_path, monkeypatch):
    _, path, _ = configured
    outside = tmp_path / "outside.json"
    outside.write_bytes(path.read_bytes())
    monkeypatch.setenv("CONTENT_PUBLISH_ANDROID_CONFIG", str(outside))
    result = android.inspect_devices()
    assert not result["configured"] and "outside.json" not in json.dumps(result)


def test_duplicate_json_keys_are_not_silently_accepted(configured):
    _, path, _ = configured
    path.write_text('{"version":1,"version":1,"devices":{}}', encoding="utf-8")
    assert not android.status()["configured"]


def test_inspection_runs_only_devices_listing_and_returns_bound_device_states(
    configured, monkeypatch
):
    config, _, _ = configured
    calls = []

    def listing(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "List of devices attached\n"
                "emulator-5554\tdevice product:private-product model:private-model transport_id:1\n"
                "127.0.0.1:62001\toffline transport_id:2\n"
                "unconfigured-private-serial\tdevice model:private-other\n"
                "192.168.1.9:5555\tdevice\n"
            ),
            stderr="private diagnostic token",
        )

    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:outside.example:5037")
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "outside.example")
    monkeypatch.setenv("ANDROID_SERIAL", "unconfigured-private-serial")
    monkeypatch.setattr(android.subprocess, "run", listing)
    result = android.inspect_devices()
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv == [config["adb_path"], "devices", "-l"]
    assert options["shell"] is False and 0 < options["timeout"] <= 10
    assert options["stdin"] == subprocess.DEVNULL
    assert options["env"]["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5037"
    assert (
        "ANDROID_ADB_SERVER_ADDRESS" not in options["env"]
        and "ANDROID_SERIAL" not in options["env"]
    )
    assert result["devices"][0]["connected"] and result["devices"][0]["state"] == "device"
    assert not result["devices"][1]["connected"] and result["devices"][1]["state"] == "offline"
    assert not result["ready"]
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize(
    "output,state",
    [
        ("List of devices attached\n", "not_found"),
        ("List of devices attached\nemulator-5554\tunauthorized\n", "unauthorized"),
        ("List of devices attached\nemulator-5554\tdevice\nemulator-5554\tdevice\n", "ambiguous"),
    ],
)
def test_absent_unauthorized_and_ambiguous_devices_are_not_connected(
    configured, monkeypatch, output, state
):
    monkeypatch.setattr(
        android.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    result = android.inspect_devices()
    assert result["devices"][0]["state"] == state and not result["devices"][0]["connected"]
    assert not result["ready"]


@pytest.mark.parametrize("failure", ["timeout", "oserror", "exit", "malformed"])
def test_probe_failures_are_sanitized_and_never_retried(configured, monkeypatch, failure):
    calls = []

    def failing(*args, **kwargs):
        calls.append(args)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                "private-adb-path", 8, output="private-output", stderr="private-secret"
            )
        if failure == "oserror":
            raise OSError("private-path and private-secret")
        return SimpleNamespace(
            returncode=1 if failure == "exit" else 0,
            stdout="private-secret",
            stderr="private-secret",
        )

    monkeypatch.setattr(android.subprocess, "run", failing)
    result = android.inspect_devices()
    assert len(calls) == 1 and result["ready"] is False
    assert all(
        device["state"] == "probe_failed" and not device["connected"]
        for device in result["devices"]
    )
    assert "private-" not in json.dumps(result)


def test_execution_always_blocks_without_device_actions_marker_or_browser_fallback(
    configured, monkeypatch
):
    monkeypatch.setattr(
        browser,
        "publish",
        lambda *_args, **_kwargs: pytest.fail("Android must not fall back to browser"),
    )
    monkeypatch.setattr(
        browser,
        "manage_listing",
        lambda *_args, **_kwargs: pytest.fail("Android must not fall back to browser"),
    )
    for platform in android.status()["platforms"]:
        with pytest.raises(browser.BrowserPublishError) as error:
            android.preflight({"platforms": [platform], "verified": True, "device_id": "main"})
        assert error.value.status == "blocked"
        with pytest.raises(browser.BrowserPublishError) as error:
            android.execute(
                platform,
                "publish",
                {},
                {"verified": True},
                device_id="main",
                before_submit=lambda: pytest.fail("Must not mark submission"),
            )
        assert error.value.status == "blocked"
