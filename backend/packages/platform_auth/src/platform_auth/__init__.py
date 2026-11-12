"""Framework-neutral backend authentication SDK for Yifabao Agent consumers."""

from .client import PlatformAuthClient
from .errors import (
    AuthenticationRequired,
    EntryUnavailable,
    InvalidGatewayIdentity,
    PlatformAuthError,
)
from .gateway import GatewayIdentityCodec
from .models import GatewayContext, Identity, SsoCredential, UserIdentity

__all__ = [
    "AuthenticationRequired",
    "EntryUnavailable",
    "GatewayContext",
    "GatewayIdentityCodec",
    "Identity",
    "InvalidGatewayIdentity",
    "SsoCredential",
    "UserIdentity",
    "PlatformAuthClient",
    "PlatformAuthError",
]
