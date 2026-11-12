"""Explicit server-operator initialization of the first project administrator.

Run with the application's environment and Python runtime after the real user has
entered through verified SSO: python -m content_service.bootstrap_admin --subject REAL
This only initializes the first administrator; later changes use the audited UI.
"""

import argparse
import json
import sys

from fastapi import HTTPException
from sqlalchemy import func, select, true

from agent_core.identity import identity_mode

from .access_control import access_error, access_transaction, stored_permissions, validate_subject
from .models import AuditLog, LocalUser, utcnow


def bootstrap_admin(subject: str) -> dict:
    validate_subject(subject)
    if identity_mode() not in {"signed", "session"}:
        raise access_error(409, "请先启用超级入口正式身份认证，再初始化项目管理员")
    if subject.casefold() == "dev-user":
        raise access_error(400, "不能将开发账号设为项目管理员，请使用真实账号的 subject")
    with access_transaction() as session:
        if session.scalar(
            select(func.count()).select_from(LocalUser).where(LocalUser.is_project_admin == true())
        ):
            raise access_error(409, "已有项目管理员，请使用人员与权限页面分配授权")
        user = session.scalar(select(LocalUser).where(LocalUser.subject == subject))
        if user is None:
            raise access_error(404, "该账号尚未登记，请先让本人通过超级入口进入内容智能体")
        stored_permissions(user)
        user.is_project_admin = True
        user.revision += 1
        user.updated_at = utcnow()
        session.add(
            AuditLog(
                owner_id="server-operator",
                action="access.admin.bootstrap",
                resource_type="local_user",
                resource_id=user.id,
                details_json=json.dumps(
                    {"subject": subject, "revision": user.revision}, ensure_ascii=False
                ),
            )
        )
        result = {"subject": subject, "display_name": user.display_name, "revision": user.revision}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="将已登记的真实账号初始化为首位项目管理员")
    parser.add_argument("--subject", required=True, help="已验证的超级入口用户唯一标识")
    args = parser.parse_args()
    try:
        result = bootstrap_admin(args.subject)
    except HTTPException as error:
        print(str(error.detail), file=sys.stderr)
        return 1
    print(f"项目管理员初始化完成：{result['display_name']}（{result['subject']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
