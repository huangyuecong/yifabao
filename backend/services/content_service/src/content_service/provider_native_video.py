from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import Artifact, ProjectAsset

PROVIDER_NATIVE_VIDEO_TRANSPORT = "provider-managed-output-https"
_UNUSABLE_ARTIFACT_STATUSES = frozenset({"discarded", "legacy_mock", "stale"})
_SAFE_PROVIDER_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


@dataclass(frozen=True, slots=True)
class ProviderNativeVideoReference:
    provider_id: str
    provider_task_id: str
    duration_seconds: float | None


def _object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _json_object(value: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return _object(parsed)


def _one_consistent_string(*values: object) -> str | None:
    normalized = {value.strip() for value in values if isinstance(value, str) and value.strip()}
    return normalized.pop() if len(normalized) == 1 else None


def _duration(*values: object) -> float | None:
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            duration = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and 0 < duration <= 600:
            return duration
    return None


def resolve_provider_native_video_reference(
    session: Session,
    item: ProjectAsset,
) -> ProviderNativeVideoReference | None:
    """Derive a provider-managed AI-video reference from authoritative records.

    ProjectAsset metadata is intentionally not trusted on its own.  A marker is
    emitted only for an active, locally archived AI video whose linked Artifact
    carries a consistent provider task identifier and provider provenance.
    """

    if not item.active or item.source_type != "artifact" or item.role != "video":
        return None
    artifact_id = item.artifact_id or item.source_id
    if not artifact_id or (item.artifact_id and item.source_id != item.artifact_id):
        return None
    artifact = session.get(Artifact, artifact_id)
    if (
        artifact is None
        or artifact.kind != "ai_video_clip"
        or artifact.status in _UNUSABLE_ARTIFACT_STATUSES
        or not artifact.storage_key
    ):
        return None

    content = _json_object(artifact.content_json)
    metadata = _json_object(item.metadata_json)
    if content.get("legacy_mock") is True:
        return None
    content_provenance = _object(content.get("provenance"))
    metadata_provenance = _object(metadata.get("provenance"))
    content_request = _object(content_provenance.get("request"))
    metadata_request = _object(metadata_provenance.get("request"))

    provider_task_id = _one_consistent_string(
        content.get("provider_task_id"),
        metadata.get("provider_task_id"),
    )
    if provider_task_id is None or _SAFE_PROVIDER_TASK_ID.fullmatch(provider_task_id) is None:
        return None
    provider_id = _one_consistent_string(
        content_provenance.get("provider_id"),
        content.get("provider"),
        content.get("provider_id"),
        metadata_provenance.get("provider_id"),
        metadata.get("provider"),
        metadata.get("provider_id"),
    )
    if provider_id is None or len(provider_id) > 128:
        return None

    return ProviderNativeVideoReference(
        provider_id=provider_id,
        provider_task_id=provider_task_id,
        duration_seconds=_duration(
            metadata.get("duration_seconds"),
            metadata_request.get("duration_seconds"),
            content.get("duration_seconds"),
            content_request.get("duration_seconds"),
        ),
    )


def provider_native_video_marker(
    reference: ProviderNativeVideoReference,
) -> dict[str, object]:
    return {
        "eligible": True,
        "provider_id": reference.provider_id,
        "transport": PROVIDER_NATIVE_VIDEO_TRANSPORT,
        "reference_kind": "content-generation-task-output",
    }
