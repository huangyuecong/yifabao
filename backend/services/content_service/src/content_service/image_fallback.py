"""Try several image models behind one provider key, in a fixed order."""

import time
from collections.abc import Sequence
from threading import Lock

from .llm_adapters import ProviderCallError
from .providers import GeneratedAsset, ImageGenerationRequest

# The same key fails the same way on every model, and an input-image rejection
# is about the request, not the model.
_NO_FALLBACK_STATUS = {401, 403}
_NO_FALLBACK_CODES = {"input_image_privacy_information"}
# After a fallback succeeds, keep starting from it for a while instead of
# waiting on the failing model for every image, then retry the primary again.
STICKY_SECONDS = 600


class FallbackImageGenerationPort:
    def __init__(self, ports: Sequence) -> None:
        if not ports:
            raise ValueError("至少需要一个图片模型")
        self.ports = list(ports)
        self._lock = Lock()
        self._sticky: tuple[int, float] | None = None

    @property
    def status(self):
        return self.ports[0].status

    @property
    def capabilities(self):
        return self.ports[0].capabilities

    @property
    def models(self) -> list[str]:
        return [port.status.model for port in self.ports]

    def __getattr__(self, name):
        # Size/ratio limits and other read-only attributes come from the primary model.
        return getattr(self.ports[0], name)

    def _order(self) -> list[int]:
        with self._lock:
            start = 0
            if self._sticky and time.monotonic() - self._sticky[1] < STICKY_SECONDS:
                start = self._sticky[0]
        indexes = list(range(len(self.ports)))
        return indexes[start:] + indexes[:start]

    def generate(self, request: ImageGenerationRequest) -> Sequence[GeneratedAsset]:
        failures = []
        for index in self._order():
            port = self.ports[index]
            try:
                result = port.generate(request)
            except ProviderCallError as exc:
                if (getattr(exc, "provider_status_code", None) in _NO_FALLBACK_STATUS
                        or getattr(exc, "code", "") in _NO_FALLBACK_CODES):
                    raise
                failures.append((port.status.model, exc))
                continue
            with self._lock:
                self._sticky = (index, time.monotonic()) if index else None
            return result
        tried = "、".join(model for model, _ in failures)
        raise ProviderCallError(
            f"已依次尝试 {tried}，均未生成成功。最后一个错误：{failures[-1][1]}",
            code="provider_unavailable",
            retryable=True,
        ) from failures[-1][1]
