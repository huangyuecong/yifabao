from pathlib import Path
from typing import BinaryIO


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError("invalid storage key")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_stream(self, key: str, stream: BinaryIO, chunk_size: int = 1024 * 1024) -> Path:
        target = self.path_for(key)
        with target.open("wb") as output:
            while chunk := stream.read(chunk_size):
                output.write(chunk)
        return target
