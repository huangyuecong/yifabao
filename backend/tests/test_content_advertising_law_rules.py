from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from content_service.advertising_law_rules import (
    ADVERTISING_LAW_RULES,
    CREATED_BY,
    SEED_ID,
    seed_advertising_law_rules,
)
from content_service.models import RuleSeedState, SensitiveRule


def test_advertising_law_catalog_covers_article_nine_and_liquor_rules():
    by_phrase = {
        phrase: (rule_type, replacement) for phrase, rule_type, replacement in ADVERTISING_LAW_RULES
    }
    for phrase in ("国家级", "最高级", "最佳", "最好", "全球第一"):
        assert by_phrase[phrase][0] == "forbidden"
    for phrase in ("第一", "唯一", "绝对"):
        assert by_phrase[phrase][0] == "sensitive"
    for phrase in ("无节制饮酒", "劝酒", "酒后驾车", "疗效", "养生保健", "驰名商标"):
        assert by_phrase[phrase][0] == "forbidden"
    assert "最" not in by_phrase
    assert "雅酿" not in by_phrase


def test_advertising_law_seed_is_idempotent_and_preserves_custom_rules():
    engine = create_engine("sqlite:///:memory:")
    SensitiveRule.__table__.create(engine)
    RuleSeedState.__table__.create(engine)
    with Session(engine) as session:
        session.add(RuleSeedState(id=SEED_ID, completed=False))
        session.add(
            SensitiveRule(
                phrase="最佳",
                rule_type="sensitive",
                level="warning",
                replacement="请保留自定义建议",
                created_by="operator",
                active=True,
            )
        )
        session.commit()
        first = seed_advertising_law_rules(session)
        session.commit()
        second = seed_advertising_law_rules(session)
        session.commit()
        assert first == len(ADVERTISING_LAW_RULES) - 1
        assert second == 0
        kept = session.scalar(select(SensitiveRule).where(SensitiveRule.phrase == "最佳"))
        assert kept is not None
        assert kept.replacement == "请保留自定义建议"
        assert kept.created_by == "operator"
        seeded = session.scalars(
            select(SensitiveRule).where(SensitiveRule.created_by == CREATED_BY)
        ).all()
        assert {row.phrase for row in seeded} == {
            phrase for phrase, _, _ in ADVERTISING_LAW_RULES if phrase != "最佳"
        }
        national = session.scalar(select(SensitiveRule).where(SensitiveRule.phrase == "国家级"))
        assert national is not None
        assert national.rule_type == "forbidden"
        assert national.level == "forbidden"
        assert national.replacement == "知名"
        session.delete(national)
        session.commit()
        # Even a fresh startup transaction must not restore a deleted default.
        assert seed_advertising_law_rules(session) == 0
        session.commit()
        assert session.scalar(select(SensitiveRule).where(SensitiveRule.phrase == "国家级")) is None
