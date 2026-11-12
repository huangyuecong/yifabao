"""Entry -> Agent signed identity wire protocol.

The wire format belongs to the consumer SDK rather than the Entry server implementation so
Agent integrations can verify it without depending on ``yifabao-entry``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from .errors import InvalidGatewayIdentity
from .models import Identity


class GatewayIdentityCodec:
    """Encode and verify the signed identity envelope used by the Entry gateway."""

    def __init__(self, secret: str, ttl_seconds: int = 60) -> None:
        if not secret:
            raise ValueError("gateway identity secret is required")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds

    def encode(self, identity: Identity) -> str:
        now = int(time.time())
        payload = {
            "sub": identity.subject,
            "name": identity.display_name,
            "claims": identity.claims,
            "iat": now,
            "exp": now + self._ttl_seconds,
        }
        body = _encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        signature = hmac.new(
            self._secret,
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{body}.{_encode(signature)}"

    def decode(self, token: str) -> Identity:
        try:
            body, encoded_signature = token.split(".", 1)
            expected = hmac.new(
                self._secret,
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
            actual = _decode(encoded_signature)
            if not hmac.compare_digest(actual, expected):
                raise InvalidGatewayIdentity("invalid gateway identity signature")
            payload: dict[str, Any] = json.loads(_decode(body))
            if int(payload["exp"]) < int(time.time()):
                raise InvalidGatewayIdentity("expired gateway identity")
            subject = str(payload["sub"])
            if not subject:
                raise InvalidGatewayIdentity("gateway identity subject is required")
            claims = payload.get("claims", {})
            if not isinstance(claims, dict):
                raise InvalidGatewayIdentity("invalid gateway identity claims")
            return Identity(subject, str(payload.get("name", subject)), claims)
        except (
            InvalidGatewayIdentity,
            KeyError,
            TypeError,
            ValueError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            if isinstance(exc, InvalidGatewayIdentity):
                raise
            raise InvalidGatewayIdentity("invalid gateway identity") from exc


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
