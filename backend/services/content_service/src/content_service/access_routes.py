"""Project administrators grant local capabilities to already authenticated users."""

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy import func, or_, select, true
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from agent_core.identity import Identity

from .access_control import (
    NO_STORE,
    PERMISSIONS,
    ContentIdentity,
    access_error,
    access_transaction,
    get_content_identity,
    has_permission,
    is_signed,
    require_project_admin,
    require_signed,
    user_document,
)
from .models import AuditLog, LocalUser, utcnow
from .standalone_access import StandaloneIdentity


class PrivateAccessRoute(APIRoute):
    """Protect identity and permission responses, including framework-generated errors."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "private, no-store"
            await send(message)

        await super().handle(scope, receive, private_send)


router = APIRouter(prefix="/api/v1/access", tags=["access"], route_class=PrivateAccessRoute)


class AccessMe(BaseModel):
    authentication_ready: bool
    subject: str
    display_name: str
    department: str
    is_project_admin: bool
    permissions: list[str]
    can_manage_users: bool
    message: str


class AccessUser(BaseModel):
    id: str
    subject: str
    display_name: str
    department: str
    is_project_admin: bool
    permissions: list[str]
    revision: int
    last_seen_at: datetime


class AccessUsers(BaseModel):
    items: list[AccessUser]
    total: int
    page: int
    page_size: int


class AccessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    is_project_admin: StrictBool
    permissions: Annotated[list[str], Field(max_length=len(PERMISSIONS))]

    @field_validator("permissions")
    @classmethod
    def valid_permissions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not set(value) <= PERMISSIONS:
            raise ValueError("请选择有效且不重复的本地权限")
        return sorted(value)


def _no_store(response: Response) -> None:
    response.headers.update(NO_STORE)


@router.get("/me", response_model=AccessMe)
def read_access_me(response: Response, identity: Identity = Depends(get_content_identity)):
    _no_store(response)
    local = isinstance(identity, StandaloneIdentity)
    ready = local or (is_signed(identity) and isinstance(identity, ContentIdentity))
    admin = ready and isinstance(identity, ContentIdentity) and identity.is_project_admin
    permissions = (
        sorted(identity.permissions)
        if ready
        else sorted(
            permission
            for permission in ("library.manage", "rules.manage", "audit.read")
            if has_permission(identity, permission)
        )
    )
    return {
        "authentication_ready": ready,
        "subject": identity.user_id,
        "display_name": identity.name,
        "department": identity.department,
        "is_project_admin": admin,
        "permissions": permissions,
        "can_manage_users": admin,
        "message": "" if ready else "人员与权限管理需通过超级入口统一登录后使用",
    }


@router.get("/users", response_model=AccessUsers)
def list_users(
    response: Response,
    keyword: Annotated[str, Query(max_length=128)] = "",
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    identity: Identity = Depends(get_content_identity),
):
    _no_store(response)
    require_signed(identity)
    with access_transaction() as session:
        require_project_admin(session, identity)
        query = select(LocalUser)
        count = select(func.count()).select_from(LocalUser)
        if keyword.strip():
            value = keyword.strip()
            match = or_(
                LocalUser.subject.contains(value, autoescape=True),
                LocalUser.display_name.contains(value, autoescape=True),
                LocalUser.department.contains(value, autoescape=True),
            )
            query = query.where(match)
            count = count.where(match)
        total = session.scalar(count)
        users = session.scalars(
            query.order_by(LocalUser.last_seen_at.desc(), LocalUser.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        result = {
            "items": [user_document(user) for user in users],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    return result


@router.put("/users/{user_id}", response_model=AccessUser)
def update_user(
    user_id: str,
    body: AccessUpdate,
    response: Response,
    request_marker: Annotated[str | None, Header(alias="X-Content-Access-Request")] = None,
    identity: Identity = Depends(get_content_identity),
):
    _no_store(response)
    require_signed(identity)
    if request_marker != "1":
        raise access_error(403, "请通过人员与权限页面提交授权修改")
    with access_transaction() as session:
        actor = require_project_admin(session, identity)
        user = session.get(LocalUser, user_id)
        if user is None:
            raise access_error(404, "用户不存在，请先通过超级入口进入本项目")
        before = user_document(user)
        if user.revision != body.expected_revision:
            raise access_error(409, "该用户权限已被修改，请刷新后重试")
        if user.is_project_admin and not body.is_project_admin:
            admin_count = session.scalar(
                select(func.count())
                .select_from(LocalUser)
                .where(LocalUser.is_project_admin == true())
            )
            if admin_count <= 1:
                raise access_error(409, "不能撤销最后一位项目管理员，请先指定另一位管理员")
        user.is_project_admin = body.is_project_admin
        user.permissions_json = json.dumps(body.permissions, ensure_ascii=False)
        user.revision += 1
        user.updated_at = utcnow()
        session.flush()
        result = user_document(user)
        details = {
            "subject": user.subject,
            "before": {key: before[key] for key in ("is_project_admin", "permissions", "revision")},
            "after": {key: result[key] for key in ("is_project_admin", "permissions", "revision")},
        }
        session.add(
            AuditLog(
                owner_id=actor.subject,
                action="access.permissions.update",
                resource_type="local_user",
                resource_id=user.id,
                details_json=json.dumps(details, ensure_ascii=False),
            )
        )
    return result
