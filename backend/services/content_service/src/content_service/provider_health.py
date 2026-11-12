from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from .llm_adapters import ProviderCallError
from .providers import ProviderStatus

# Status follows actual calls: failure persists until a successful retry.
# Health remains informational and never gates a retry.


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    status: str
    checked_at: datetime
    message: str | None = None


_health: dict[tuple[str, str, str | None], ProviderHealth] = {}
_lock = Lock()


def _key(status: ProviderStatus, configuration: str | None = None) -> tuple[str, str, str | None]:
    return status.provider_id, status.capability, configuration


def _safe_error(error: Exception) -> str:
    message = str(error).replace("\r", " ").replace("\n", " ").strip()
    message = re.sub(
        r"(?i)(authorization)\s*[:=;]?\s*(?:bearer\s*;?\s*)?\S+",
        r"\1 [已隐藏]",
        message,
    )
    message = re.sub(
        r"(?i)(bearer|access[_ -]?token|token|api[_ -]?key|secret)\s*[:=;]?\s*\S+",
        r"\1 [已隐藏]",
        message,
    )
    message = re.sub(r"https?://[^\s]+", "[服务地址已隐藏]", message)
    summary = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return summary[:240]


def record_success(status: ProviderStatus, configuration: str | None = None) -> None:
    if not status.external:
        return
    with _lock:
        key = _key(status, configuration)
        _health[key] = ProviderHealth(
            status="available",
            checked_at=datetime.now(UTC),
        )


def record_failure(
    status: ProviderStatus, error: Exception, configuration: str | None = None
) -> None:
    if not status.external:
        return
    if status.capability == "knowledge-search":
        from .weknora import WeKnoraError

        if isinstance(error, WeKnoraError) and error.code in {
            "selected_resource_denied", "selected_resource_not_indexed", "authorization_changed",
        }:
            # Local authorization or indexing readiness says nothing about
            # whether the remote service is reachable.
            return
    if (
        isinstance(error, ProviderCallError)
        and error.code == "input_image_privacy_information"
        and error.status_code == 422
    ):
        # The provider is reachable and correctly rejected unsafe user input;
        # this is not a service outage and must not poison the health indicator.
        record_success(status, configuration)
        return
    with _lock:
        key = _key(status, configuration)
        _health[key] = ProviderHealth(
            status="unavailable",
            checked_at=datetime.now(UTC),
            message=_safe_error(error),
        )


def current_health(
    status: ProviderStatus, configuration: str | None = None
) -> ProviderHealth | None:
    with _lock:
        return _health.get(_key(status, configuration))


class HealthTrackedProvider:
    """Transparent edge wrapper that records real provider call outcomes."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self.health_configuration: str | None = None

    @property
    def status(self) -> ProviderStatus:
        return self._provider.status

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._provider.capabilities

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._provider, name)
        if not callable(value) or name.startswith("_"):
            return value

        def tracked(*args: Any, **kwargs: Any) -> Any:
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                record_failure(self.status, exc, self.health_configuration)
                raise
            record_success(self.status, self.health_configuration)
            return result

        return tracked


def track_provider(provider: Any) -> Any:
    if provider.status.external:
        return HealthTrackedProvider(provider)
    return provider
