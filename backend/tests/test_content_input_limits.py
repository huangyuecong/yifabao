"""Regression tests for content-service input and upload guardrails."""

from io import BytesIO

import pytest
from fastapi import UploadFile
from pydantic import ValidationError

from content_service.main import validate_library_upload
from content_service.schemas import (
    MAX_METADATA_BYTES,
    LibraryItemCreate,
    TaskCreate,
)


def test_task_name_rejects_blank_and_overlong_values() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(name="   ")
    with pytest.raises(ValidationError):
        TaskCreate(name="x" * 256)


def test_task_text_and_keyword_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(name="task", topic="x" * 2_001)
    with pytest.raises(ValidationError):
        TaskCreate(name="task", keywords=["x"] * 51)
    with pytest.raises(ValidationError):
        TaskCreate(name="task", keywords=["x" * 129])


def test_library_metadata_size_depth_and_array_limits() -> None:
    with pytest.raises(ValidationError):
        LibraryItemCreate(category="docs", name="item", metadata={"values": list(range(101))})
    nested: object = "leaf"
    for _ in range(9):
        nested = {"next": nested}
    with pytest.raises(ValidationError):
        LibraryItemCreate(category="docs", name="item", metadata=nested)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LibraryItemCreate(
            category="docs",
            name="item",
            metadata={"value": "x" * MAX_METADATA_BYTES},
        )


def test_library_upload_rejects_unsupported_type_and_empty_file() -> None:
    unsupported = UploadFile(filename="payload.exe", file=BytesIO(b"x"))
    with pytest.raises(Exception, match="不支持的资料文件类型"):
        validate_library_upload(unsupported)
    empty = UploadFile(filename="empty.txt", file=BytesIO(b""))
    with pytest.raises(Exception, match="不能为空"):
        validate_library_upload(empty)
