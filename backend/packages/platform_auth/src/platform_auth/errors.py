"""Stable errors exposed by the Yifabao backend authentication SDK."""


class PlatformAuthError(RuntimeError):
    """Base class for backend SDK failures."""


class AuthenticationRequired(PlatformAuthError):
    """The supplied credential does not resolve to an active platform session."""


class EntryUnavailable(PlatformAuthError):
    """The central Entry authentication service cannot be reached."""


class InvalidGatewayIdentity(PlatformAuthError, ValueError):
    """A projected Entry -> Agent identity envelope is invalid or expired."""
