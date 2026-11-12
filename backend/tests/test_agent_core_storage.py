from io import BytesIO
from pathlib import Path

import pytest

from agent_core.storage import LocalObjectStorage


def test_storage_writes_within_root(tmp_path: Path):
    store = LocalObjectStorage(tmp_path / "objects")
    path = store.write_stream("folder/clip.bin", BytesIO(b"payload"))
    assert path.read_bytes() == b"payload"
    assert path.is_relative_to(store.root)


def test_storage_rejects_keys_outside_root(tmp_path: Path):
    store = LocalObjectStorage(tmp_path / "objects")
    with pytest.raises(ValueError, match="invalid storage key"):
        store.path_for("../escape.bin")
