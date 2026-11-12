"""Stateless HTTP client for the Yifabao authentication service."""

from __future__ import annotations

from typing import Any

import httpx

from .errors import AuthenticationRequired, EntryUnavailable, PlatformAuthError
from .models import GatewayContext, Identity, SsoCredential


class PlatformAuthClient:
    """Network-only authentication SDK.

    The client owns no session, credential, ACL, filesystem, or database state.
    """

    def __init__(
        self,
        base_url: str,
        *,
        gateway_secret: str = "",
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._gateway_secret = gateway_secret
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @staticmethod
    def _identity(payload: dict[str, Any]) -> Identity:
        user = payload.get("user")
        if not isinstance(user, dict) or not user.get("subject"):
            raise PlatformAuthError("authentication service returned an invalid identity")
        claims = {
            key: value
            for key, value in user.items()
            if key not in {"subject", "display_name"}
        }
        subject = str(user["subject"])
        return Identity(
            subject=subject,
            display_name=str(user.get("display_name") or subject),
            claims=claims,
        )

    async def resolve_access_token(
        self,
        access_token: str,
        *,
        tenant_id: str | None = None,
    ) -> Identity:
        if not access_token:
            raise PlatformAuthError("access token is required")
        headers = {"Authorization": f"Bearer {access_token}"}
        if self._gateway_secret:
            headers["X-Entry-Gateway-Secret"] = self._gateway_secret
        params = {"tenantId": tenant_id} if tenant_id else None
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    "/api/auth/internal/access-token",
                    headers=headers,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise EntryUnavailable("authentication service is unavailable") from exc
        if response.status_code != 200:
            raise AuthenticationRequired(
                "access token is not associated with an active session"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlatformAuthError("authentication service returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformAuthError("authentication service returned an invalid response")
        return self._identity(payload)

    async def resolve_session(self, cookie_header: str) -> GatewayContext:
        """Resolve an incoming browser session through Entry.

        The raw Cookie header is forwarded as-is so the SDK does not need to know Entry's
        configured session-cookie name. The returned SSO credential is privileged context and
        should only be requested by backend integrations that explicitly need it.
        """

        if not cookie_header.strip():
            raise AuthenticationRequired("session cookie is required")
        headers = {"Cookie": cookie_header}
        if self._gateway_secret:
            headers["X-Entry-Gateway-Secret"] = self._gateway_secret
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    "/api/auth/internal/session",
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise EntryUnavailable("authentication service is unavailable") from exc
        if response.status_code != 200:
            raise AuthenticationRequired(
                "session is not associated with active SSO credentials"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlatformAuthError("authentication service returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformAuthError("authentication service returned an invalid response")
        access_token = payload.get("accessToken")
        tenant_id = payload.get("tenantId")
        if not isinstance(access_token, str) or not access_token:
            raise PlatformAuthError("authentication service returned an invalid SSO credential")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise PlatformAuthError("authentication service returned an invalid SSO tenant")
        return GatewayContext(
            identity=self._identity(payload),
            sso_credential=SsoCredential(
                access_token=access_token,
                tenant_id=tenant_id,
            ),
        )
