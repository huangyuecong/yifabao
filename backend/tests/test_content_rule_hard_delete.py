import json

import pytest
from sqlalchemy import select
from test_content_local_permission_scopes import business as business
from test_content_local_permission_scopes import grant, headers
from test_content_local_permission_scopes import manager as manager
from test_content_local_permission_scopes import signed as signed

from content_service import db
from content_service.advertising_law_rules import SEED_ID, seed_advertising_law_rules
from content_service.generation import compliance_result
from content_service.models import AuditLog, RuleSeedState, SensitiveRule


@pytest.mark.parametrize("rule_type", ["keyword", "sensitive", "forbidden"])
def test_rule_is_physically_deleted_and_can_be_recreated(business, rule_type):
    grant(business, "rules-editor", ["rules.manage"])
    identity = headers("rules-editor")
    payload = {"phrase": "国家级", "rule_type": rule_type}
    created = business.post("/api/v1/sensitive-rules", headers=identity, json=payload)
    assert created.status_code == 200, created.text
    rule_id = created.json()["id"]
    with db.SessionLocal() as session:
        # Ensure even a catalog whose initial seeding was disabled stays deleted.
        if session.get(RuleSeedState, SEED_ID) is None:
            session.add(RuleSeedState(id=SEED_ID, completed=False))
        previous_check = compliance_result(session, {"title": "国家级宣传", "body": "正文"})
        log = AuditLog(owner_id="rules-editor", action="compliance.test", resource_type="task",
                       resource_id="historical-task", details_json=json.dumps(previous_check))
        session.add(log)
        session.commit()
        log_id = log.id
    result = business.delete(f"/api/v1/sensitive-rules/{rule_id}", headers=identity)
    assert result.status_code == 200, result.text
    with db.SessionLocal() as session:
        assert session.get(SensitiveRule, rule_id) is None
        assert session.scalar(select(SensitiveRule).where(SensitiveRule.phrase == payload["phrase"])) is None
        assert json.loads(session.get(AuditLog, log_id).details_json) == previous_check
        deletion = session.scalar(select(AuditLog).where(
            AuditLog.action == "sensitive_rule.deleted", AuditLog.resource_id == rule_id,
        ))
        assert json.loads(deletion.details_json)["deletion_mode"] == "hard"
        assert seed_advertising_law_rules(session) == 0
        session.commit()
        assert session.scalar(select(SensitiveRule).where(SensitiveRule.phrase == "国家级")) is None
    recreated = business.post("/api/v1/sensitive-rules", headers=identity, json=payload)
    assert recreated.status_code == 200
    assert recreated.json()["created"] is True
    assert recreated.json()["id"] != rule_id
    assert business.delete(f"/api/v1/sensitive-rules/{rule_id}", headers=identity).status_code == 404


def test_rule_hard_delete_still_requires_local_rules_permission(business):
    grant(business, "rules-editor", ["rules.manage"])
    result = business.post("/api/v1/sensitive-rules", headers=headers("rules-editor"),
                           json={"phrase": "不可越权删除"})
    rule_id = result.json()["id"]
    denied = business.delete(f"/api/v1/sensitive-rules/{rule_id}", headers=headers("ordinary"))
    assert denied.status_code == 403
    with db.SessionLocal() as session:
        assert session.get(SensitiveRule, rule_id) is not None
