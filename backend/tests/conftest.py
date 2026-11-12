import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(tempfile.mkdtemp(prefix="yifabao-content-tests-"))
os.environ["CONTENT_DATA_DIR"] = str(TEST_ROOT / "content")
os.environ["CONTENT_MUSIC_PROVIDER"] = "mock"
os.environ["CONTENT_IDENTITY_MODE"] = os.environ.get("CONTENT_IDENTITY_MODE", "development")
os.environ["CONTENT_SEED_ADVERTISING_LAW"] = "false"
# Existing chat tests script provider replies in order; streaming and tool routing
# make extra calls, so they are enabled only in the tests written for them.
os.environ["CONTENT_CHAT_STREAMING"] = "false"
os.environ["CONTENT_CHAT_TOOL_ROUTING"] = "false"
# Placeholder HiAgent tenant: the gateway host and its object-storage port.
os.environ["HIAGENT_BASE_URL"] = "https://hiagent.example.com:30040"
os.environ["HIAGENT_OBJECT_STORAGE_PORT"] = "30873"

for path in [
    ROOT / "packages" / "agent_core" / "src",
    ROOT / "packages" / "platform_auth" / "src",
    ROOT / "services" / "content_service" / "src",
    Path(__file__).resolve().parent,
]:
    sys.path.insert(0, str(path))


@pytest.fixture(scope="session")
def content_client():
    from content_service.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def user_headers():
    return {
        "X-User-Id": "user-a",
        "X-User-Name": "user-a",
        "X-Department": "department-a",
        "X-Roles": "user,reviewer,admin",
    }


@pytest.fixture
def other_headers():
    return {
        "X-User-Id": "user-b",
        "X-User-Name": "user-b",
        "X-Department": "department-b",
        "X-Roles": "user",
    }
