"""Wire models returned by the Yifabao authentication service."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    subject: str
    display_name: str = ""
    claims: dict[str, object] = field(default_factory=dict)

    @property
    def department(self) -> str | None:
        value = self.claims.get("department")
        return value if isinstance(value, str) else None

    @property
    def roles(self) -> tuple[str, ...]:
        value = self.claims.get("roles", ())
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(item for item in value if isinstance(item, str))
        return ()

    @property
    def permissions(self) -> tuple[str, ...]:
        value = self.claims.get("permissions", ())
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(item for item in value if isinstance(item, str))
        return ()


@dataclass(slots=True, frozen=True)
class UserIdentity:
    id: str
    name: str
    avatar_url: str | None = None
    department_id: str | None = None
    clearance: str = "public"
    roles: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SsoCredential:
    """Privileged customer SSO credential projected by Entry for an allowed request."""

    access_token: str
    tenant_id: str


@dataclass(slots=True, frozen=True)
class GatewayContext:
    """Framework-neutral trusted context projected from Entry to an Agent backend."""

    identity: Identity
    sso_credential: SsoCredential | None = None
