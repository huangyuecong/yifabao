"""Remove obsolete attached summaries without changing knowledge-library summaries."""
from typing import Any


def clean_text_content(value: Any) -> Any:
    if isinstance(value, list):
        return [clean_text_content(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: item for key, item in value.items() if key != "summary"}
    for key in ("candidates", "candidate", "selected_candidate", "source_content", "source_candidate", "compliance"):
        if key in result:
            result[key] = clean_text_content(result[key])
    if isinstance(result.get("hits"), list):
        hits = result["hits"]
        result["hits"] = [
            hit for hit in hits
            if not isinstance(hit, dict) or str(hit.get("field", "")).split(".")[-1] != "summary"
        ]
        if len(result["hits"]) != len(hits) and "passed" in result:
            result["passed"] = not any(hit.get("rule_type") == "forbidden" for hit in result["hits"] if isinstance(hit, dict))
    return result


def artifact_content(kind: str, value: Any) -> Any:
    return clean_text_content(value) if kind in {"topic", "copy", "compliance"} else value
