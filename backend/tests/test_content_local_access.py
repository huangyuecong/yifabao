import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker
from test_content_entry_identity import signed as signed
from test_content_entry_identity import token

from agent_core.identity import Identity, verify_entry_identity
from content_service import access_control as access
from content_service import db
from content_service.access_routes import router
from content_service.bootstrap_admin import bootstrap_admin
from content_service.models import AuditLog, LocalAccessLock, LocalUser

URL = "/api/v1/access"


def headers(subject="alice", **claims):
    return {
        "X-Entry-Identity": token(
            subject=subject,
            claims={
                "agent_keys": ["CONTENT-TEST"],
                "enabled": True,
                "department": "市场部",
                **claims,
            },
        ),
        "X-Content-Access-Request": "1",
    }


@pytest.fixture
def local_app(tmp_path, monkeypatch, signed):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'permissions.db'}", connect_args={"check_same_thread": False}
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    for model in (LocalAccessLock, LocalUser, AuditLog):
        model.__table__.create(engine)
    with factory.begin() as session:
        session.add(LocalAccessLock(id=1))
    monkeypatch.setattr(db, "SessionLocal", factory)
    app = FastAPI()
    app.include_router(router)

    @app.get("/effective")
    def effective(identity: Identity = Depends(access.get_content_identity)):
        return {
            "roles": sorted(identity.roles),
            "permissions": sorted(identity.permissions),
            "models": access.has_permission(identity, "models.manage"),
        }

    with TestClient(app) as client:
        yield client, factory, app
    engine.dispose()


def register(client, subject="alice", **claims):
    response = client.get(URL + "/me", headers=headers(subject, **claims))
    assert response.status_code == 200, response.text
    return response.json()


def create_admin(client, subject="alice"):
    register(client, subject)
    bootstrap_admin(subject)


def user_row(factory, subject):
    with factory() as session:
        return session.scalar(select(LocalUser).where(LocalUser.subject == subject))


def put(client, row, actor="alice", *, admin=False, permissions=None, revision=None):
    return client.put(
        URL + "/users/" + row.id,
        headers=headers(actor),
        json={
            "expected_revision": row.revision if revision is None else revision,
            "is_project_admin": admin,
            "permissions": permissions or [],
        },
    )


def test_first_signed_visit_registers_without_entry_grants(local_app, monkeypatch):
    client, factory, _ = local_app
    monkeypatch.setenv("CONTENT_IDENTITY_ROLE_MAP", '{"superadmin":["admin"]}')
    monkeypatch.setenv("CONTENT_IDENTITY_PERMISSION_MAP", '{"everything":["admin"]}')
    grant = headers(
        roles=["superadmin"],
        permissions=["everything"],
        is_admin=True,
        manageable_agent_keys=["CONTENT-TEST"],
    )
    grant.update({"X-Roles": "admin", "X-Permissions": "models.manage", "X-User-Id": "forged"})
    result = client.get(URL + "/me", headers=grant)
    assert result.status_code == 200
    assert result.json() == {
        "authentication_ready": True,
        "subject": "alice",
        "display_name": "联调用户",
        "department": "市场部",
        "is_project_admin": False,
        "permissions": [],
        "can_manage_users": False,
        "message": "",
    }
    assert result.headers["cache-control"] == "private, no-store"
    assert client.get("/effective", headers=grant).json() == {
        "roles": ["user"],
        "permissions": [],
        "models": False,
    }
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(LocalUser)) == 1
    assert client.get(URL + "/users", headers=grant).status_code == 403


def test_local_permission_survives_later_login_and_profile_updates(local_app):
    client, factory, _ = local_app
    create_admin(client)
    register(client, "bob")
    old = user_row(factory, "bob")
    response = put(client, old, permissions=["models.manage"])
    assert response.status_code == 200
    fresh_headers = headers("bob", department="新部门", is_admin=False, manageable_agent_keys=[])
    fresh_headers["X-Entry-Identity"] = token(
        subject="bob",
        name="新姓名",
        claims={
            "department": "新部门",
            "agent_keys": ["CONTENT-TEST"],
            "is_admin": False,
            "manageable_agent_keys": [],
        },
    )
    result = client.get(URL + "/me", headers=fresh_headers).json()
    assert result["permissions"] == ["models.manage"]
    assert not result["is_project_admin"] and not result["can_manage_users"]
    current = user_row(factory, "bob")
    assert current.id == old.id and current.revision == 2
    assert current.display_name == "新姓名" and current.department == "新部门"
    assert current.last_seen_at >= old.last_seen_at
    assert client.get("/effective", headers=headers("bob")).json() == {
        "roles": ["user"],
        "permissions": ["models.manage"],
        "models": True,
    }


def test_grant_and_revoke_take_effect_next_request_and_are_audited(local_app):
    client, factory, _ = local_app
    create_admin(client)
    register(client, "bob")
    row = user_row(factory, "bob")
    assert put(client, row, permissions=["models.manage", "library.manage"]).status_code == 200
    assert client.get("/effective", headers=headers("bob")).json()["models"]
    assert put(client, user_row(factory, "bob")).status_code == 200
    assert not client.get("/effective", headers=headers("bob")).json()["models"]
    with factory() as session:
        logs = session.scalars(
            select(AuditLog).where(AuditLog.action == "access.permissions.update")
        ).all()
        assert len(logs) == 2 and all(log.owner_id == "alice" for log in logs)
        assert json.loads(logs[-1].details_json)["after"]["permissions"] == []
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "access.admin.bootstrap")
            )
            == 1
        )


def test_users_requires_admin_and_supports_search_pagination(local_app):
    client, factory, _ = local_app
    create_admin(client)
    register(client, "bob")
    register(client, "bob%")
    assert client.get(URL + "/users", headers=headers("bob")).status_code == 403
    assert client.get(URL + "/users?keyword=bob%25", headers=headers()).json()["total"] == 1
    result = client.get(URL + "/users?page=1&page_size=2", headers=headers())
    assert result.status_code == 200
    assert result.json()["total"] == 3 and len(result.json()["items"]) == 2
    assert result.headers["cache-control"] == "private, no-store"
    assert client.get(URL + "/users?page_size=1000", headers=headers()).status_code == 422


def test_write_header_and_expected_revision_protect_updates(local_app):
    client, factory, _ = local_app
    create_admin(client)
    register(client, "bob")
    row = user_row(factory, "bob")
    body = {
        "expected_revision": row.revision,
        "is_project_admin": False,
        "permissions": ["models.manage"],
    }
    missing_header = headers()
    del missing_header["X-Content-Access-Request"]
    assert (
        client.put(URL + "/users/" + row.id, headers=missing_header, json=body).status_code == 403
    )
    assert put(client, row, permissions=["models.manage"]).status_code == 200
    stale = put(client, row, permissions=["library.manage"])
    assert stale.status_code == 409 and stale.headers["cache-control"] == "private, no-store"
    assert json.loads(user_row(factory, "bob").permissions_json) == ["models.manage"]


@pytest.mark.parametrize(
    "body",
    [
        {"permissions": ["models.manage", "models.manage"]},
        {"permissions": ["content:ai:manage"]},
        {"permissions": ["admin"]},
        {"permissions": [5]},
        {"is_project_admin": "true"},
        {"expected_revision": True},
        {"expected_revision": 0},
        {"subject": "forged"},
    ],
)
def test_malformed_grants_rejected(local_app, body):
    client, factory, _ = local_app
    create_admin(client)
    register(client, "bob")
    row = user_row(factory, "bob")
    payload = {"expected_revision": 1, "is_project_admin": False, "permissions": [], **body}
    assert client.put(URL + "/users/" + row.id, headers=headers(), json=payload).status_code == 422
    assert user_row(factory, "bob").revision == 1


def test_ordinary_user_cannot_self_promote_or_discover_unknown_users(local_app):
    client, factory, _ = local_app
    register(client, "bob", is_admin=True, manageable_agent_keys=["CONTENT-TEST"])
    row = user_row(factory, "bob")
    assert put(client, row, "bob", admin=True).status_code == 403
    assert (
        client.put(
            URL + "/users/not-real",
            headers=headers("bob"),
            json={
                "expected_revision": 1,
                "is_project_admin": True,
                "permissions": [],
            },
        ).status_code
        == 403
    )
    assert not user_row(factory, "bob").is_project_admin


def test_last_admin_cannot_be_removed_and_can_transfer_authority(local_app):
    client, factory, _ = local_app
    create_admin(client)
    assert put(client, user_row(factory, "alice")).status_code == 409
    register(client, "bob")
    assert put(client, user_row(factory, "bob"), admin=True).status_code == 200
    assert put(client, user_row(factory, "alice"), "bob").status_code == 200
    assert client.get(URL + "/users", headers=headers()).status_code == 403
    assert client.get(URL + "/users", headers=headers("bob")).status_code == 200
    assert put(client, user_row(factory, "bob"), "bob").status_code == 409


def test_cached_identity_cannot_retain_revoked_user_admin_authority(local_app):
    client, factory, app = local_app
    create_admin(client)
    register(client, "bob")
    assert put(client, user_row(factory, "bob"), admin=True).status_code == 200
    raw = verify_entry_identity(headers()["X-Entry-Identity"])
    cached = access.get_content_identity(raw)
    assert cached.is_project_admin
    assert put(client, user_row(factory, "alice"), "bob").status_code == 200
    app.dependency_overrides[access.get_content_identity] = lambda: cached
    assert client.get(URL + "/users", headers=headers()).status_code == 403
    assert put(client, user_row(factory, "alice"), admin=True).status_code == 403


def test_concurrent_first_visits_register_single_identity(local_app):
    _, factory, _ = local_app
    raw = verify_entry_identity(headers()["X-Entry-Identity"])
    gate = Barrier(4)

    def visit():
        gate.wait(timeout=10)
        return access.get_content_identity(raw).local_user_id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: visit(), range(4)))
    assert len(set(ids)) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(LocalUser)) == 1


def test_concurrent_admin_demotions_leave_an_admin(local_app):
    client, factory, _ = local_app
    create_admin(client)
    register(client, "bob")
    put(client, user_row(factory, "bob"), admin=True)
    rows = [user_row(factory, "alice"), user_row(factory, "bob")]
    gate = Barrier(2)

    def demote(row):
        gate.wait(timeout=10)
        return put(client, row, row.subject).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(demote, rows))
    assert statuses == [200, 409]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(LocalUser)
                .where(LocalUser.is_project_admin.is_(True))
            )
            == 1
        )


def test_bootstrap_only_existing_signed_user_and_only_once(local_app, monkeypatch):
    client, factory, _ = local_app
    with pytest.raises(HTTPException) as error:
        bootstrap_admin("missing")
    assert error.value.status_code == 404
    register(client, "alice")
    assert bootstrap_admin("alice")["revision"] == 2
    with pytest.raises(HTTPException) as error:
        bootstrap_admin("alice")
    assert error.value.status_code == 409
    assert user_row(factory, "alice").revision == 2
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    with pytest.raises(HTTPException) as error:
        bootstrap_admin("alice")
    assert error.value.status_code == 409


def test_concurrent_bootstrap_creates_exactly_one_admin(local_app):
    client, factory, _ = local_app
    register(client, "alice")
    register(client, "bob")
    gate = Barrier(2)

    def initialize(subject):
        gate.wait(timeout=10)
        try:
            bootstrap_admin(subject)
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(initialize, ["alice", "bob"])) == [200, 409]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(LocalUser)
                .where(LocalUser.is_project_admin.is_(True))
            )
            == 1
        )


def test_development_cannot_manage_or_register_users(local_app, monkeypatch):
    client, factory, _ = local_app
    monkeypatch.setenv("CONTENT_IDENTITY_MODE", "development")
    raw_headers = {"X-Roles": "admin", "X-User-Id": "dev-user", "X-Content-Access-Request": "1"}
    result = client.get(URL + "/me", headers=raw_headers)
    assert result.status_code == 200
    assert not result.json()["authentication_ready"] and not result.json()["can_manage_users"]
    assert result.json()["permissions"] == ["audit.read", "library.manage", "rules.manage"]
    assert not result.json()["is_project_admin"]
    ordinary = client.get(URL + "/me", headers={"X-Roles": "user", "X-User-Id": "other-dev"})
    assert ordinary.json()["permissions"] == []
    assert client.get(URL + "/users", headers=raw_headers).status_code == 403
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(LocalUser)) == 0
    # Keep existing standalone business checks compatible; new admin surfaces stay locked.
    assert client.get("/effective", headers=raw_headers).json()["roles"] == ["admin"]
    assert not client.get("/effective", headers=raw_headers).json()["models"]


@pytest.mark.parametrize("subject", ["x" * 129, " alice", "alice ", "a\nb", "a\x7fb"])
def test_invalid_subject_does_not_register(local_app, subject):
    client, factory, _ = local_app
    assert client.get(URL + "/me", headers=headers(subject)).status_code == 401
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(LocalUser)) == 0


def test_failed_signature_or_revoked_entry_access_never_registers(local_app):
    client, factory, _ = local_app
    assert client.get(URL + "/me", headers={"X-Roles": "admin"}).status_code == 401
    invalid = headers()
    invalid["X-Entry-Identity"] = token(secret="bad")
    assert client.get(URL + "/me", headers=invalid).status_code == 401
    assert client.get(URL + "/me", headers=headers(enabled=False)).status_code == 403
    assert client.get(URL + "/me", headers=headers(agent_keys=[])).status_code == 403
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(LocalUser)) == 0


@pytest.mark.parametrize("value", ["bad", "{}", '["unknown"]', '["models.manage","models.manage"]'])
def test_invalid_persisted_permissions_fail_closed(local_app, value):
    client, factory, _ = local_app
    create_admin(client)
    with factory.begin() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == "alice"))
        user.permissions_json = value
    result = client.get(URL + "/me", headers=headers())
    assert result.status_code == 503
    assert client.get(URL + "/users", headers=headers()).status_code == 503


def test_raw_entry_admin_is_not_local_authority(local_app):
    raw = verify_entry_identity(headers(is_admin=True)["X-Entry-Identity"])
    raw = replace(raw, roles=frozenset({"admin"}), permissions=access.PERMISSIONS)
    assert not access.has_permission(raw, "models.manage")
    assert not access.has_permission(raw, "unexpected")


def test_signed_dev_subject_cannot_bootstrap(local_app):
    client, _, _ = local_app
    register(client, "dev-user")
    with pytest.raises(HTTPException) as error:
        bootstrap_admin("dev-user")
    assert error.value.status_code == 400


def test_missing_migration_is_reported_without_leaking_database_details(local_app):
    client, factory, _ = local_app
    with factory.begin() as session:
        session.delete(session.get(LocalAccessLock, 1))
    result = client.get(URL + "/me", headers=headers())
    assert result.status_code == 503 and "尚未初始化" in result.text


def test_repeated_identity_reads_do_not_lock_or_write_but_read_latest_permissions(
    local_app, monkeypatch
):
    client, factory, _ = local_app
    register(client)
    original_seen = user_row(factory, "alice").last_seen_at

    def unexpected_write():
        pytest.fail("Fresh identity reads must not enter a serialized write transaction")

    monkeypatch.setattr(access, "access_transaction", unexpected_write)
    assert register(client)["permissions"] == []
    with factory.begin() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == "alice"))
        user.permissions_json = '["models.manage"]'
        user.revision += 1
    assert register(client)["permissions"] == ["models.manage"]
    assert user_row(factory, "alice").last_seen_at == original_seen


def test_last_seen_is_refreshed_without_changing_permission_revision(local_app):
    client, factory, _ = local_app
    register(client)
    with factory.begin() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == "alice"))
        user.last_seen_at -= timedelta(seconds=61)
    old = user_row(factory, "alice")
    register(client)
    current = user_row(factory, "alice")
    assert current.last_seen_at > old.last_seen_at
    assert current.revision == old.revision


def test_migration_preserves_business_data_and_does_not_invent_local_users(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'upgrade.db'}"
    monkeypatch.setenv("CONTENT_DATABASE_URL", database_url)
    config = Config(
        str(Path(__file__).resolve().parents[1] / "services/content_service/alembic.ini")
    )
    command.upgrade(config, "content_0013")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_logs (id, owner_id, action, resource_type, resource_id, "
                "details_json, created_at) VALUES ('old-event', 'dev-user', 'old.action', "
                "'task', 'old-task', '{}', CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(config, "head")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM local_users")).scalar() == 0
        assert connection.execute(text("SELECT id FROM local_access_lock")).scalar() == 1
        assert (
            connection.execute(
                text("SELECT owner_id FROM audit_logs WHERE id = 'old-event'")
            ).scalar()
            == "dev-user"
        )
    command.downgrade(config, "content_0013")
    assert "local_users" not in inspect(engine).get_table_names()
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM audit_logs WHERE id = 'old-event'")
            ).scalar()
            == 1
        )
    engine.dispose()


def test_auth_and_validation_errors_also_disable_caching(local_app):
    client, _, _ = local_app
    unsigned = client.get(URL + "/me")
    assert unsigned.status_code == 401
    assert unsigned.headers["cache-control"] == "private, no-store"
    create_admin(client)
    invalid = client.get(URL + "/users?page_size=1000", headers=headers())
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "private, no-store"
    invalid_body = client.put(URL + "/users/not-real", headers=headers(), json={})
    assert invalid_body.status_code == 422
    assert invalid_body.headers["cache-control"] == "private, no-store"
