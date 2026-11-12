from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import ClassVar
from uuid import uuid4

import pytest

from content_service.llm_adapters import ProviderCallError
from content_service.main import get_service_status
from content_service.provider_health import (
    current_health,
    record_failure,
    record_success,
    track_provider,
)
from content_service.providers import ProviderStatus


def _external_status(*, capability: str = "image-generation") -> ProviderStatus:
    return ProviderStatus(
        provider_id=f"health-test-{uuid4()}",
        name="health-test",
        capability=capability,
        mode="api",
        external=True,
        enabled=True,
    )


def test_failed_provider_status_persists_until_success_recovers():
    class ExternalProvider:
        status = _external_status()
        capabilities: ClassVar[dict] = {}

        def __init__(self):
            self.should_fail = True

        def generate(self):
            if self.should_fail:
                raise ConnectionError("temporary provider failure")
            return "ok"

    raw = ExternalProvider()
    provider = track_provider(raw)

    with pytest.raises(ConnectionError, match="temporary provider failure"):
        provider.generate()

    failed = current_health(raw.status)
    assert failed is not None
    assert failed.status == "unavailable"

    assert current_health(raw.status).status == "unavailable"

    retryable = current_health(raw.status)
    assert retryable is not None
    assert retryable.status == "unavailable"
    assert retryable.checked_at == failed.checked_at
    assert "temporary provider failure" in (retryable.message or "")

    raw.should_fail = False
    assert provider.generate() == "ok"
    recovered = current_health(raw.status)
    assert recovered is not None
    assert recovered.status == "available"
    assert recovered.message is None


def test_repeated_failure_updates_the_reported_reason():
    status = _external_status()

    record_failure(status, RuntimeError("first failure"))
    record_failure(status, RuntimeError("second failure"))

    health = current_health(status)
    assert health is not None
    assert health.status == "unavailable"
    assert "second failure" in (health.message or "")

    assert current_health(status).status == "unavailable"


def test_input_privacy_rejection_does_not_mark_provider_unavailable():
    status = _external_status(capability="ai-video-generation")

    record_failure(status, ConnectionError("temporary provider failure"))
    assert current_health(status).status == "unavailable"
    record_failure(
        status,
        ProviderCallError(
            "technical provider detail",
            code="input_image_privacy_information",
            retryable=False,
            status_code=422,
        ),
    )

    health = current_health(status)
    assert health is not None
    assert health.status == "available"
    assert health.message is None


def test_concurrent_health_readers_cannot_overwrite_a_newer_success():
    status = _external_status()
    record_failure(status, RuntimeError("temporary failure"))

    reader_count = 16
    barrier = Barrier(reader_count + 1)

    def read_health() -> str:
        barrier.wait()
        health = current_health(status)
        assert health is not None
        return health.status

    def report_success() -> None:
        barrier.wait()
        record_success(status)

    with ThreadPoolExecutor(max_workers=reader_count + 1) as executor:
        readers = [executor.submit(read_health) for _ in range(reader_count)]
        success = executor.submit(report_success)
        observed = {future.result() for future in readers}
        success.result()

    assert observed <= {"unavailable", "available"}
    assert current_health(status).status == "available"


def test_service_status_starts_available_and_tracks_failure_recovery():
    class Port:
        def __init__(self, status: ProviderStatus):
            self.status = status

    capabilities = (
        "text-generation",
        "knowledge-search",
        "image-generation",
        "speech-synthesis",
        "music-generation",
        "subtitle-alignment",
        "ai-video-generation",
        "video-composition",
        "timeline-video-composition",
    )
    ports = [
        Port(
            _external_status(capability=capability)
            if capability == "image-generation"
            else ProviderStatus(
                provider_id=f"local-{capability}",
                name=f"local-{capability}",
                capability=capability,
                mode="local",
                external=False,
                enabled=True,
            )
        )
        for capability in capabilities
    ]
    assert get_service_status(None, *ports)["overall"] == "available"
    image_status = ports[2].status
    record_failure(image_status, ConnectionError("provider is temporarily unreachable"))

    unavailable_response = get_service_status(None, *ports)
    unavailable_item = unavailable_response["services"][2]
    assert set(unavailable_item) == {
        "capability",
        "provider_id",
        "status",
        "checked_at",
        "message",
    }
    assert unavailable_item["status"] == "unavailable"

    retryable_response = get_service_status(None, *ports)
    retryable_item = retryable_response["services"][2]
    assert set(retryable_response) == {"overall", "services"}
    assert retryable_response["overall"] == "degraded"
    assert retryable_item["status"] == "unavailable"
    assert retryable_item["checked_at"] == unavailable_item["checked_at"]

    record_success(image_status)
    assert get_service_status(None, *ports)["overall"] == "available"
