"""Platform capabilities, separate from whether a live publishing adapter is ready.

Registering an app exposes workflow configuration; it never enables publishing.
Executors own account checks, operation support, and verified submission receipts.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    id: str
    label: str
    purpose: str
    operations: tuple[str, ...]
    execution_modes: tuple[str, ...]
    requires_video: bool
    title_limit: int = 30
    description_limit: int = 1000


PLATFORMS = {
    spec.id: spec
    for spec in (
        PlatformSpec(
            "xianyu",
            "闲鱼",
            "listing",
            ("publish", "relist", "delist"),
            ("browser",),
            False,
        ),
        PlatformSpec("douyin", "抖音", "promotion", ("publish",), ("browser",), True),
        # Promotion workflows currently produce video drafts. Image-note
        # publishing needs its own verified web form and publication receipt.
        PlatformSpec(
            "xiaohongshu", "小红书", "promotion", ("publish",), ("browser",), True, title_limit=20
        ),
        PlatformSpec("kuaishou", "快手", "promotion", ("publish",), ("browser",), True),
    )
}


def get_platform(platform: str) -> PlatformSpec:
    try:
        return PLATFORMS[platform]
    except (KeyError, TypeError):
        raise ValueError("平台尚未注册，请先添加平台能力与执行适配器") from None


def catalog_document() -> list[dict]:
    return [asdict(spec) for spec in PLATFORMS.values()]
