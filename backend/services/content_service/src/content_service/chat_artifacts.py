"""Owner-scoped generated files; no filesystem paths are accepted from clients."""

import os
import re
import shutil
from pathlib import Path

from fastapi import HTTPException

from .chat_storage import ChatStore


def artifact_directory(store: ChatStore, artifact_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
        raise HTTPException(404, "生成文件不存在")
    return store.root / "artifacts" / artifact_id


def artifact_path(store: ChatStore, artifact) -> Path:
    directory = artifact_directory(store, artifact["id"])
    path = directory / artifact["filename"]
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise HTTPException(410, "生成文件已丢失，请重新生成")
    return path


def remove_artifact_directory(store: ChatStore, artifact_id: str) -> None:
    directory = artifact_directory(store, artifact_id)
    if directory.exists():
        shutil.rmtree(directory)


def prepare_artifact(store: ChatStore, artifact_id: str, result: dict) -> dict:
    directory = artifact_directory(store, artifact_id)
    path = Path(result["path"])
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise ValueError("Generated file is outside its private directory")
    size = path.stat().st_size
    if not 0 < size <= 100 * 1024 * 1024:
        raise HTTPException(422, "生成文件为空或超过 100 MB，请缩小生成范围")
    if os.name != "nt":
        os.chmod(directory, 0o700)
        os.chmod(path, 0o600)
    return {**result, "id": artifact_id, "size": size, "filename": path.name}
