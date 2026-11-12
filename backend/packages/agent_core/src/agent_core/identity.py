import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Header, HTTPException, Request
from platform_auth import GatewayIdentityCodec
from platform_auth.errors import InvalidGatewayIdentity


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: str
    name: str
    department: str
    roles: frozenset[str]
    clearance: str = "public"
    permissions: frozenset[str] = frozenset()
    entry_is_admin: bool | None = None
    manageable_agent_keys: frozenset[str] | None = None
    entry_agent_key: str = ""
    entry_verified: bool = False

    def has_role(self, role: str) -> bool:
        return role in self.roles


def identity_mode() -> str:
    # Existing standalone installations remain compatible until coordinated cutover.
    mode = os.getenv("CONTENT_IDENTITY_MODE", "development").strip()
    if mode not in {"development", "signed", "session"}:
        raise HTTPException(503, "身份认证模式配置无效")
    return mode


def _invalid() -> HTTPException:
    return HTTPException(401, "统一身份无效或已过期，请从超级入口重新进入")


def _decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def verify_entry_identity(token: str | None) -> Identity:
    """Verify the entry gateway identity; never fall back to browser-supplied roles.

    The supplied 20260922 SDK owns the standard base64url signing protocol.
    Retain stricter payload/time checks and our explicit business role mapping.
    """
    try:
        secret_file = (
            os.getenv("CONTENT_IDENTITY_SECRET_FILE", "")
            or os.getenv("XJ_AUTH_GATEWAY_SECRET_FILE", "")
        ).strip()
        secret = (
            Path(secret_file).read_text(encoding="utf-8").strip()
            if secret_file
            else os.getenv("CONTENT_IDENTITY_SECRET", os.getenv("XJ_AUTH_GATEWAY_SECRET", ""))
        )
        signing_input = os.getenv("CONTENT_IDENTITY_SIGNING_INPUT", "base64url")
        agent_key = os.getenv("CONTENT_ENTRY_AGENT_KEY", "").strip()
        max_age = int(os.getenv("CONTENT_IDENTITY_MAX_TTL_SECONDS", "60"))
        clock_skew = int(os.getenv("CONTENT_IDENTITY_CLOCK_SKEW_SECONDS", "5"))
        require_grants = os.getenv("CONTENT_IDENTITY_REQUIRE_AGENT_KEYS", "false").lower()
        role_map = json.loads(
            os.getenv("CONTENT_IDENTITY_ROLE_MAP", '{"employee":["user"],"user":["user"]}')
        )
        permission_map = json.loads(os.getenv("CONTENT_IDENTITY_PERMISSION_MAP", "{}"))
        if (
            len(secret.encode("utf-8")) < 32
            or not agent_key
            or signing_input not in {"base64url", "json"}
            or not 1 <= max_age <= 3600
            or not 0 <= clock_skew <= 60
            or require_grants not in {"true", "false"}
            or not isinstance(role_map, dict)
            or not isinstance(permission_map, dict)
            or not all(
                isinstance(key, str)
                and _strings(value)
                and set(value) <= {"user", "reviewer", "admin"}
                for key, value in [*role_map.items(), *permission_map.items()]
            )
        ):
            raise ValueError("Invalid identity configuration")
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, "下游身份签名配置未就绪") from None

    if not token:
        raise _invalid()
    try:
        if len(token) > 16384:
            raise ValueError("Oversized identity")
        encoded_payload, encoded_signature = token.split(".")
        payload_bytes = _decode(encoded_payload)
        signature = _decode(encoded_signature)
        signed = encoded_payload.encode("ascii") if signing_input == "base64url" else payload_bytes
        expected = hmac.digest(secret.encode("utf-8"), signed, hashlib.sha256)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signature")
        payload = json.loads(payload_bytes, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError("Invalid payload")
        subject = payload.get("sub")
        name, claims = payload.get("name", subject), payload.get("claims", {})
        issued, expires = payload.get("iat"), payload.get("exp")
        now = time.time()
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or not isinstance(name, str)
            or not isinstance(claims, dict)
            or type(issued) is not int
            or type(expires) is not int
            or issued > now + clock_skew
            or expires <= now
            or not 0 < expires - issued <= max_age
        ):
            raise ValueError("Invalid identity fields")
        department = claims.get("department")
        clearance = claims.get("clearance", "public")
        source_roles = claims.get("roles", [])
        source_permissions = claims.get("permissions", [])
        if isinstance(source_roles, str):
            source_roles = [source_roles]
        if isinstance(source_permissions, str):
            source_permissions = [source_permissions]
        if (
            department is not None
            and not isinstance(department, str)
            or not isinstance(clearance, str)
            or not clearance.strip()
            or not _strings(source_roles)
            or not _strings(source_permissions)
            or "enabled" in claims
            and type(claims["enabled"]) is not bool
            or "agent_keys" in claims
            and not _strings(claims["agent_keys"])
            or "is_admin" in claims
            and type(claims["is_admin"]) is not bool
            or "manageable_agent_keys" in claims
            and not _strings(claims["manageable_agent_keys"])
        ):
            raise ValueError("Invalid claims")
        if signing_input == "base64url":
            sdk_identity = GatewayIdentityCodec(secret, ttl_seconds=max_age).decode(token)
            subject = sdk_identity.subject
            name = sdk_identity.display_name
    except (InvalidGatewayIdentity, ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid() from None

    if claims.get("enabled") is False:
        raise HTTPException(403, "当前用户已停用")
    if "agent_keys" in claims:
        if agent_key not in claims["agent_keys"]:
            raise HTTPException(403, "无权访问此智能体")
    elif require_grants == "true":
        raise HTTPException(403, "缺少此智能体的访问授权")
    roles = frozenset(
        [role for source in source_roles for role in role_map.get(source, [])]
        + [role for source in source_permissions for role in permission_map.get(source, [])]
    )
    return Identity(
        subject,
        name.strip() or subject,
        department or "",
        roles,
        clearance,
        frozenset(source_permissions),
        entry_is_admin=claims.get("is_admin"),
        manageable_agent_keys=(
            frozenset(claims["manageable_agent_keys"])
            if "manageable_agent_keys" in claims
            else None
        ),
        entry_agent_key=agent_key,
        entry_verified=True,
    )


def get_identity(
    x_user_id: str = Header(default="dev-user", alias="X-User-Id"),
    x_user_name: str = Header(default="开发用户", alias="X-User-Name"),
    x_department: str = Header(default="数字与信息化中心", alias="X-Department"),
    x_roles: str = Header(default="user,reviewer,admin", alias="X-Roles"),
    x_entry_identity: Annotated[str | None, Header(alias="x-entry-identity")] = None,
    request: Request = None,
) -> Identity:
    mode = identity_mode()
    if mode == "development" and os.getenv("CONTENT_STANDALONE_MODE", "false").strip().lower() == "true":
        # The content-service dependency verifies the local request boundary and
        # assigns capabilities. Browser identity headers never select a local owner.
        return Identity("dev-user", "本机用户", "", frozenset({"user", "reviewer", "admin"}))
    if mode == "session":
        from .entry_session import resolve_entry_session

        return resolve_entry_session(request)
    if mode == "signed" or x_entry_identity is not None:
        return verify_entry_identity(x_entry_identity)
    roles = frozenset(value.strip() for value in x_roles.split(",") if value.strip())
    return Identity(x_user_id, x_user_name, x_department, roles)
