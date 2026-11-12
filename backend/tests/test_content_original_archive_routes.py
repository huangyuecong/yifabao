from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from agent_core import Identity
from content_service import weknora_routes
from content_service.config import load_settings
from content_service.db import get_session
from content_service.models import AuditLog, Base, LibraryItem, OriginalArchive, ResourceEntry


@pytest.fixture
def archive_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    config = replace(load_settings(), knowledge_provider="library_db", weknora_originals_enabled=True,
                     weknora_base_url="https://knowledge.example.test", weknora_api_key="test-key",
                     weknora_knowledge_base_id="kb-1")
    monkeypatch.setattr(weknora_routes, "get_runtime_settings", lambda: config)
    monkeypatch.setattr(weknora_routes, "get_weknora_client", lambda: SimpleNamespace(connection_id="test-connection"))
    app = FastAPI()
    app.include_router(weknora_routes.router)

    def session():
        with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session
    actor = {"id": "alice", "permissions": frozenset()}
    app.dependency_overrides[weknora_routes.get_content_identity] = lambda: Identity(
        user_id=actor["id"], name=actor["id"], department="", roles=frozenset(),
        permissions=actor["permissions"],
    )
    with factory() as db:
        entry = ResourceEntry(id="entry", owner_id="alice", resource_kind="document", title="资料",
                              category="brand", data_level="internal")
        db.add(entry)
        db.flush()
        db.add(LibraryItem(id="item", owner_id="alice", name="资料",
                           category="brand", text_content="正文", data_level="internal",
                           resource_entry_id=entry.id, storage_key="library/original.txt"))
        db.add(OriginalArchive(storage_key="library/original.txt", entry_id=entry.id,
                               connection_id="test-connection", knowledge_base_id="kb-1",
                               checksum_sha256="a" * 64, size_bytes=10, source_marker="owned-marker",
                               remote_id="private-remote-id", status="failed"))
        db.commit()
    with TestClient(app) as client:
        yield client, actor, factory, config, app
    engine.dispose()


def test_archive_status_and_retry_require_library_ownership(archive_api):
    client, actor, factory, _, _ = archive_api
    actor["id"] = "bob"
    assert client.get("/api/v1/library/items/item/knowledge-sync").status_code == 403
    assert client.post("/api/v1/library/items/item/original-archive/retry").status_code == 403
    with factory() as db:
        assert db.scalar(select(OriginalArchive)).status == "failed"
    actor["id"] = "alice"
    response = client.get("/api/v1/library/items/item/knowledge-sync")
    assert response.status_code == 200
    assert response.json()["status"] == "local"
    assert response.json()["original"]["status"] == "failed"
    assert "private-remote-id" not in response.text and "library/original.txt" not in response.text
    assert client.post("/api/v1/library/items/item/original-archive/retry").status_code == 200
    with factory() as db:
        assert db.scalar(select(OriginalArchive)).status == "queued"
        assert db.scalar(select(AuditLog)).action == "knowledge.original_retried"


def test_archive_retry_respects_disabled_and_data_level(archive_api, monkeypatch):
    client, _, factory, config, _ = archive_api
    monkeypatch.setattr(weknora_routes, "get_runtime_settings", lambda: replace(config, weknora_originals_enabled=False))
    assert client.get("/api/v1/library/items/item/knowledge-sync").json()["original"]["can_retry"] is False
    assert client.post("/api/v1/library/items/item/original-archive/retry").status_code == 409
    monkeypatch.setattr(weknora_routes, "get_runtime_settings", lambda: config)
    with factory() as db:
        db.get(LibraryItem, "item").data_level = "restricted"
        db.commit()
    assert client.post("/api/v1/library/items/item/original-archive/retry").status_code == 403


def test_file_authorization_precedes_store_access_and_download_is_private(archive_api, tmp_path):
    from content_service import main

    client, actor, _, _, app = archive_api
    app.add_api_route("/test-download/{item_id}", main.download_library_item)
    calls = []
    path = tmp_path / "original.txt"
    path.write_bytes(b"original bytes")

    def get_path(key):
        calls.append(key)
        return path

    app.dependency_overrides[main.get_asset_store_port] = lambda: SimpleNamespace(path_for=get_path)
    actor["id"] = "bob"
    assert client.get("/test-download/item").status_code == 403
    assert calls == []
    actor["id"] = "alice"
    response = client.get("/test-download/item")
    assert response.status_code == 200
    assert response.content == b"original bytes"
    assert response.headers["cache-control"] == "private, no-store"


def test_original_archive_settings_require_credentials_even_with_local_retrieval(monkeypatch):
    for name in ("CONTENT_WEKNORA_BASE_URL", "CONTENT_WEKNORA_API_KEY", "CONTENT_WEKNORA_API_KEY_FILE",
                 "CONTENT_WEKNORA_KNOWLEDGE_BASE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTENT_KNOWLEDGE_PROVIDER", "library_db")
    monkeypatch.setenv("CONTENT_WEKNORA_ORIGINALS_ENABLED", "true")
    with pytest.raises(ValueError, match="API Key"):
        load_settings()
