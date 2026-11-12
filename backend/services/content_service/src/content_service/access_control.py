"""Resolve business authority locally after Entry authenticates an allowed user."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent_core.identity import Identity, get_identity, identity_mode

from . import db
from .models import LocalAccessLock, LocalUser, utcnow
from .standalone_access import StandaloneIdentity, standalone_enabled
from .standalone_access import local_identity as standalone_identity

PERMISSIONS = frozenset({"models.manage", "library.manage", "rules.manage", "audit.read"})
NO_STORE = {"Cache-Control": "private, no-store"}


def access_error(status: int, message: str) -> HTTPException:
    return HTTPException(status, message, headers=NO_STORE)


@dataclass(frozen=True, slots=True)
class ContentIdentity(Identity):
    local_user_id: str = ""
    is_project_admin: bool = False


def is_signed(identity: Identity) -> bool:
    # Keep the existing helper name for callers; either configured SSO transport
    # must have verified both the real account and its admission to this agent.
    return identity_mode() in {"signed", "session"} and identity.entry_verified


def require_signed(identity: Identity) -> None:
    if not is_signed(identity):
        raise access_error(403, "人员与权限管理需通过超级入口统一登录后使用")


def validate_subject(subject: str) -> str:
    # Owner IDs in the existing business tables have the same 128-character limit.
    # Never truncate/normalize identity keys: distinct upstream accounts must stay distinct.
    if (
        not isinstance(subject, str)
        or not 1 <= len(subject) <= 128
        or subject != subject.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in subject)
    ):
        raise access_error(401, "统一身份的用户标识格式无效，请联系管理员")
    return subject


def stored_permissions(user: LocalUser) -> frozenset[str]:
    try:
        value = json.loads(user.permissions_json)
        if (
            not isinstance(value, list)
            or not all(isinstance(item, str) and item in PERMISSIONS for item in value)
            or len(value) != len(set(value))
        ):
            raise ValueError()
        return frozenset(value)
    except (ValueError, TypeError, RecursionError):
        raise access_error(503, "本地权限数据无效，请联系项目管理员") from None


@contextmanager
def access_transaction() -> Iterator[Session]:
    """Serialize access policy writes in the database, across processes, not just threads."""
    try:
        with db.SessionLocal() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            lock = session.scalar(
                select(LocalAccessLock).where(LocalAccessLock.id == 1).with_for_update()
            )
            if lock is None:
                raise access_error(503, "本地权限数据库尚未初始化，请完成版本升级")
            yield session
            session.commit()
    except SQLAlchemyError:
        raise access_error(503, "本地权限服务暂不可用，请稍后重试") from None


def local_identity(raw: Identity, user: LocalUser) -> ContentIdentity:
    permissions = stored_permissions(user)
    values = {field.name: getattr(raw, field.name) for field in fields(Identity)}
    values.update(
        roles=frozenset({"user", "admin"} if user.is_project_admin else {"user"}),
        permissions=PERMISSIONS if user.is_project_admin else permissions,
    )
    return ContentIdentity(**values, local_user_id=user.id, is_project_admin=user.is_project_admin)


def get_content_identity(identity: Identity = Depends(get_identity), request: Request = None) -> Identity:
    if standalone_enabled():
        return standalone_identity(request, PERMISSIONS)
    if identity_mode() == "development":
        return identity
    require_signed(identity)
    subject = validate_subject(identity.user_id)
    if len(identity.name) > 255 or len(identity.department) > 255:
        raise access_error(401, "统一身份的姓名或部门字段过长，请联系管理员")
    # Polling task/media endpoints must not serialize every request as a DB writer.
    # Authority is still read fresh on every request; only profile/seen timestamps
    # are throttled. Recheck again inside the serialized transaction before writes.
    try:
        with db.SessionLocal() as session:
            current = session.scalar(select(LocalUser).where(LocalUser.subject == subject))
            if current is not None and not _needs_profile_update(current, identity):
                return local_identity(identity, current)
    except SQLAlchemyError:
        raise access_error(503, "本地权限服务暂不可用，请稍后重试") from None
    with access_transaction() as session:
        user = session.scalar(select(LocalUser).where(LocalUser.subject == subject))
        if user is None:
            user = LocalUser(
                subject=subject,
                display_name=identity.name,
                department=identity.department,
                is_project_admin=False,
                permissions_json="[]",
                revision=1,
            )
            session.add(user)
        elif _needs_profile_update(user, identity):
            # A later login updates profile fields; never reset or import authorization.
            user.display_name = identity.name
            user.department = identity.department
            user.last_seen_at = utcnow()
        session.flush()
        result = local_identity(identity, user)
    return result


def _needs_profile_update(user: LocalUser, identity: Identity) -> bool:
    return (
        user.display_name != identity.name
        or user.department != identity.department
        or (utcnow() - user.last_seen_at).total_seconds() >= 60
    )


def has_permission(identity: Identity, permission: str) -> bool:
    if permission not in PERMISSIONS:
        return False
    if isinstance(identity, StandaloneIdentity) and standalone_enabled():
        return permission in identity.permissions
    if identity_mode() == "development":
        return permission != "models.manage" and identity.has_role("admin")
    return (
        isinstance(identity, ContentIdentity)
        and identity.entry_verified
        and (identity.is_project_admin or permission in identity.permissions)
    )


def can_view_all_tasks(identity: Identity) -> bool:
    """Only a verified, locally assigned project admin may inspect all task history."""
    return (
        isinstance(identity, ContentIdentity)
        and is_signed(identity)
        and identity.is_project_admin
    )


def require_project_admin(session: Session, identity: Identity) -> LocalUser:
    """Read authority from the same transaction as mutations; do not trust a stale dependency."""
    require_signed(identity)
    user = session.scalar(select(LocalUser).where(LocalUser.subject == identity.user_id))
    if user is None or not user.is_project_admin:
        raise access_error(403, "当前账号没有本项目的人员授权管理权限")
    stored_permissions(user)
    return user


def user_document(user: LocalUser) -> dict:
    permissions = stored_permissions(user)
    return {
        "id": user.id,
        "subject": user.subject,
        "display_name": user.display_name,
        "department": user.department,
        "is_project_admin": user.is_project_admin,
        "permissions": sorted(PERMISSIONS if user.is_project_admin else permissions),
        "revision": user.revision,
        "last_seen_at": user.last_seen_at,
    }
