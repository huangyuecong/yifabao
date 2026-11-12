"""Baseline phrases for the 关键词 / 敏感词 / 禁用词 library.

Drawn from the Advertising Law of the PRC:
- Article 9(3) absolute-claim language (国家级、最高级、最佳 and similar)
- Article 9 also prohibits advertising a mark as a well-known trademark (驰名商标)
- Article 23 liquor advertising (no encouragement of excessive drinking, drink-driving)
- Typical unsubstantiated medical / health claims that liquor ads must not make

Single-character 「最」 is omitted so everyday words such as 最后 / 最近 are not flagged.
Existing custom phrases are never overwritten.
"""

from __future__ import annotations

import os
from typing import Literal

from sqlalchemy import false, select, update
from sqlalchemy.orm import Session

from .models import RuleSeedState, SensitiveRule

RuleType = Literal["keyword", "sensitive", "forbidden"]

CREATED_BY = "system:advertising-law"
SEED_ID = "advertising-law-v1"

# phrase, rule_type, suggested rewrite shown to the editor (never auto-applied)
ADVERTISING_LAW_RULES: tuple[tuple[str, RuleType, str | None], ...] = (
    ("国家级", "forbidden", "知名"),
    ("世界级", "forbidden", "享誉业界"),
    ("最高级", "forbidden", "高端"),
    ("最佳", "forbidden", "优秀"),
    ("最好", "forbidden", "很好"),
    ("最优", "forbidden", "出色"),
    ("最强", "forbidden", "出色"),
    ("最优秀", "forbidden", "优秀"),
    ("第一品牌", "forbidden", "知名品牌"),
    ("全国第一", "forbidden", "领先"),
    ("全球第一", "forbidden", "广受关注"),
    ("世界第一", "forbidden", "广受关注"),
    ("全网最低", "forbidden", "优惠"),
    ("最低价", "forbidden", "优惠价"),
    ("第一", "sensitive", "领先"),
    ("唯一", "sensitive", "特色"),
    ("首个", "sensitive", "较早推出"),
    ("首家", "sensitive", "特色"),
    ("独家", "sensitive", "特色"),
    ("冠军", "sensitive", "优异"),
    ("绝对", "sensitive", "确实"),
    ("驰名商标", "forbidden", None),
    ("国家级奖项", "sensitive", "行业奖项"),
    ("无节制饮酒", "forbidden", None),
    ("劝酒", "forbidden", None),
    ("酒后驾车", "forbidden", None),
    ("醉酒驾驶", "forbidden", None),
    ("治疗疾病", "forbidden", None),
    ("疗效", "forbidden", None),
    ("治愈", "forbidden", None),
    ("保健功效", "forbidden", None),
    ("药用价值", "forbidden", None),
    ("养生保健", "forbidden", None),
    ("绝对安全", "forbidden", "品质可靠"),
)


def advertising_law_seed_enabled() -> bool:
    raw = os.getenv("CONTENT_SEED_ADVERTISING_LAW", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def seed_advertising_law_rules(session: Session) -> int:
    # The migration creates this singleton. Claim it in the same transaction as
    # the inserts so concurrent workers cannot reseed or revive deleted rules.
    claimed = session.execute(
        update(RuleSeedState)
        .where(RuleSeedState.id == SEED_ID, RuleSeedState.completed == false())
        .values(completed=True)
    )
    if claimed.rowcount != 1:
        return 0
    existing = set(session.scalars(select(SensitiveRule.phrase)).all())
    added = 0
    for phrase, rule_type, replacement in ADVERTISING_LAW_RULES:
        if phrase in existing:
            continue
        level = "forbidden" if rule_type == "forbidden" else "warning"
        session.add(
            SensitiveRule(
                phrase=phrase,
                rule_type=rule_type,
                level=level,
                replacement=replacement,
                created_by=CREATED_BY,
                active=True,
            )
        )
        existing.add(phrase)
        added += 1
    return added
