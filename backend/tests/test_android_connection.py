"""Connection reuse and recovery without connecting to a real Android device."""

from types import SimpleNamespace

import adbutils
import pytest
import uiautomator2

from content_service import android_connection as connection
from content_service.automation_browser import BrowserPublishError


@pytest.fixture
def setup_connection(monkeypatch):
    connection._clients.clear()
    config = {
        "adb_path": "test-adb",
        "adb_server_port": 15038,
        "devices": {"main": {"serial": "127.0.0.1:15556"}},
    }
    device = SimpleNamespace(get_state=lambda: "device")
    client = SimpleNamespace(
        list=lambda: [SimpleNamespace(serial="127.0.0.1:15556", state="device")],
        device=lambda _: device,
    )
    monkeypatch.setattr(adbutils, "AdbClient", lambda **_: client)
    commands = []
    monkeypatch.setattr(connection, "adb", lambda _, args, **kwargs: commands.append(args))
    yield config, client, device, commands
    connection._clients.clear()


def test_live_screen_reuses_control_client_without_spawning_adb(setup_connection, monkeypatch):
    config, _, _, commands = setup_connection
    initialized = []
    monkeypatch.setattr(uiautomator2, "connect", lambda device: initialized.append(device) or object())
    first = connection.connect(config, "main")
    assert connection.connect(config, "main") is first
    assert len(initialized) == 1
    assert commands == []


def test_offline_connection_retries_transport_and_discards_cached_client(setup_connection, monkeypatch):
    config, client, device, commands = setup_connection
    monkeypatch.setattr(uiautomator2, "connect", lambda _: object())
    connection.connect(config, "main")
    client.list = list
    device.get_state = lambda: "offline"
    with pytest.raises(BrowserPublishError) as error:
        connection.connect(config, "main")
    assert error.value.code == "android_device_offline"
    assert not connection._clients
    assert commands == [["start-server"], ["connect", "127.0.0.1:15556"]]


def test_control_failure_is_not_misreported_as_network_failure(setup_connection, monkeypatch):
    config, _, _, _ = setup_connection

    def unavailable(_):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(uiautomator2, "connect", unavailable)
    with pytest.raises(BrowserPublishError) as error:
        connection.connect(config, "main")
    assert error.value.code == "android_control_unavailable"
    assert "private diagnostic" not in str(error.value)
    assert not connection._clients


def test_changed_adb_server_does_not_reuse_another_devices_client(setup_connection, monkeypatch):
    config, _, _, _ = setup_connection
    monkeypatch.setattr(uiautomator2, "connect", lambda _: object())
    first = connection.connect(config, "main")
    config["adb_server_port"] = 15039
    assert connection.connect(config, "main") is not first
