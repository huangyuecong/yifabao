from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from content_service.generation import compliance_result
from content_service.models import SensitiveRule


def _session_with_phrase(phrase: str, rule_type: str = "forbidden") -> Session:
    engine = create_engine("sqlite:///:memory:")
    SensitiveRule.__table__.create(engine)
    session = Session(engine)
    session.add(
        SensitiveRule(
            phrase=phrase,
            rule_type=rule_type,
            level="forbidden" if rule_type == "forbidden" else "warning",
            created_by="operator",
            active=True,
        )
    )
    session.commit()
    return session


def test_compliance_ignores_product_library_titles_and_keeps_copy_clean():
    session = _session_with_phrase("雅酿")
    result = compliance_result(
        session,
        {
            "title": "岁月沉淀，举杯共话情谊",
            "body": "53°小坛酒，窖藏系列，1.5L 大容量。",
            "references": [
                {
                    "id": "product-1",
                    "title": "53°雅酿·小坛酒（1.5L）",
                    "category": "雅酿",
                    "data_level": "internal",
                }
            ],
        },
    )
    assert result["passed"] is True
    assert result["hits"] == []


def test_compliance_returns_preceding_sentence_and_highlights_the_phrase():
    session = _session_with_phrase("雅酿")
    body = "晚辈的举杯间透着敬意。53°雅酿·小坛酒（1.5L）大容量，盛得下时光的沉淀。不喧哗，自有声。"
    result = compliance_result(
        session,
        {
            "candidates": [{"title": "岁月沉淀，举杯共话情谊", "body": body}],
            "selected_candidate": {"title": "岁月沉淀，举杯共话情谊", "body": body},
            "references": [
                {"id": "product-1", "title": "53°雅酿·小坛酒（1.5L）", "data_level": "internal"}
            ],
        },
    )
    assert result["passed"] is False
    assert len(result["hits"]) == 1
    hit = result["hits"][0]
    assert hit["phrase"] == "雅酿"
    assert hit["match"] == "雅酿"
    assert hit["field_label"] == "候选文案 1 · 文案正文"
    assert hit["prefix"].endswith("53°")
    assert "晚辈的举杯间透着敬意。" in hit["prefix"]
    assert hit["suffix"].startswith("·小坛酒")
    assert "不喧哗" not in hit["context"]
