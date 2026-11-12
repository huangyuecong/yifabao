"""Business product identity is independent of an attachment's media type."""

import json


def is_product_resource(entry) -> bool:
    if entry is None:
        return False
    try:
        metadata = json.loads(entry.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return (
        entry.resource_kind == "product"
        or entry.category in {"product", "产品资料"}
        or isinstance(metadata, dict)
        and metadata.get("resource_kind") == "product"
    )
