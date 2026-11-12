"""Model settings use local business grants after Entry authenticates the user."""

from dataclasses import dataclass

from fastapi import Request

from agent_core import Identity

from .access_control import has_permission, is_signed
from .standalone_access import StandaloneIdentity, require_local_request, standalone_enabled


@dataclass(frozen=True)
class ManagementAccess:
    can_manage: bool
    authentication_ready: bool
    message: str


def management_access(identity: Identity, request: Request) -> ManagementAccess:
    if standalone_enabled() and isinstance(identity, StandaloneIdentity):
        require_local_request(request)
        return ManagementAccess(True, True, "")
    # The shared identity dependency authenticates and checks agent admission.
    # Entry admin fields never grant this application's business permissions.
    if not is_signed(identity):
        return ManagementAccess(False, False, "模型配置需通过超级入口统一登录后使用")
    allowed = has_permission(identity, "models.manage")
    return ManagementAccess(
        allowed,
        True,
        "" if allowed else "当前账号没有模型管理权限，请联系本项目管理员授权",
    )
