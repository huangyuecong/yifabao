"""File locations for catalog products, generated drafts and publish packages.

Generated and publish files live under data_dir/automation-media because the
browser publishers only accept files from there. Paths stored in the database
are relative to that root.
"""

from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image, ImageOps

from .provider_dependencies import get_asset_store_port, get_runtime_settings

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def media_root() -> Path:
    root = (get_runtime_settings().data_dir / "automation-media").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_work_dir(*parts: str) -> Path:
    directory = media_root().joinpath(*parts, uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def relative(path: Path) -> str:
    return path.resolve().relative_to(media_root()).as_posix()


def absolute(value: str) -> Path:
    root = media_root()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "文件已失效，请重新生成")
    return path


def product_image_path(storage_key: str) -> Path:
    try:
        path = get_asset_store_port().path_for(storage_key).resolve()
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(503, "商品图片暂时无法读取") from None
    if not path.is_file():
        raise HTTPException(404, "商品图片文件缺失")
    return path


def save_jpeg(source: Path | bytes, target: Path) -> Path:
    """Normalize any supported image to an upright RGB JPEG, max 2400px."""
    import io

    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((2400, 2400))
            image.save(target, format="JPEG", quality=92)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "图片文件损坏或尺寸过大") from None
    return target


def media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return IMAGE_TYPES.get(suffix) or VIDEO_TYPES.get(suffix) or "application/octet-stream"
