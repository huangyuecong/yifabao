"""Claim-once execution with durable receipts at the irreversible publishing boundary."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import select, update

from agent_core import Identity

from . import automation_review as review
from . import db
from .automation_models import AutomationEvent, AutomationRun
from .automation_platforms import get_platform
from .models import AuditLog, utcnow

ACTIVE = ("queued", "preparing", "publishing")
LEASE_SECONDS = 180
OPERATION_LABELS = {"publish": "发布", "relist": "上架", "delist": "下架"}
TRANSACTION_FIELDS = frozenset({"price", "condition", "shipping_fee", "location"})


def business_config(config: dict, *, normalize_destination=False, validate_destination=True) -> dict:
    """Single-purpose workflows never share a publishing draft across businesses."""
    result = dict(config)
    if result.get("purpose") == "creation":
        if result.get("platforms") or result.get("accounts") or result.get("operation", "publish") != "publish":
            raise ValueError("产品创作独立于平台发布，请在入库后选择发布平台")
        result.update(platforms=[], accounts={}, execution_mode="browser", device_id=None,
                      auto_publish=False, require_review=True, price=None, shipping_fee="0", condition="", location="")
        return result
    platforms = result.get("platforms")
    if (not isinstance(platforms, list) or not 1 <= len(platforms) <= 8
        or any(not isinstance(platform, str) for platform in platforms)
        or len(platforms) != len(set(platforms))):
        raise ValueError("请选择不重复的发布平台")
    try:
        specs = [get_platform(platform) for platform in platforms]
    except (ValueError, KeyError):
        raise ValueError("所选发布平台尚未注册") from None
    if any(spec is None for spec in specs):
        raise ValueError("所选发布平台尚未注册")
    purposes = {spec.purpose for spec in specs}
    if len(purposes) != 1:
        raise ValueError("商品上架与视频宣传必须拆分为独立工作流，请选择同一业务的平台")
    inferred = next(iter(purposes))
    purpose = result.get("purpose") or inferred
    operation = result.get("operation", "publish")
    if purpose != inferred or any(operation not in spec.operations for spec in specs):
        raise ValueError("业务用途与平台或操作不匹配")
    execution_mode = result.get("execution_mode", "browser")
    expected_mode = "browser"
    if execution_mode not in {"browser", "android"}:
        raise ValueError("执行方式无效")
    original_device = result.get("device_id")
    if original_device is not None and (not isinstance(original_device, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", original_device)):
        raise ValueError("设备标识无效，请选择本地配置的设备")
    if normalize_destination and execution_mode != expected_mode:
        # Only an explicit save can migrate a workflow. Existing run snapshots
        # must never silently send an Android draft through a browser account.
        execution_mode = expected_mode
        result["accounts"] = {}
        result["device_id"] = None
    if validate_destination and execution_mode != expected_mode:
        raise ValueError("执行方式已调整：闲鱼商品上架和内容宣传均使用网页。请重新保存工作流并生成新草稿。")
    device_id = result.get("device_id")
    if device_id is not None and (not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id)):
        raise ValueError("设备标识无效，请选择本地配置的设备")
    if normalize_destination and execution_mode == "browser":
        device_id = None
    elif execution_mode == "browser" and device_id is not None:
        raise ValueError("浏览器执行不使用安卓设备")
    result["purpose"] = purpose
    result["execution_mode"] = execution_mode
    result["device_id"] = device_id
    if purpose == "promotion":
        result.update(price=None, condition="", shipping_fee="0", location="")
    return result


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def event(session, run, kind, message, platform=None):
    session.add(AutomationEvent(run_id=run.id, kind=kind, message=message, platform=platform))


def audit(session, owner, action, resource_id, details=None):
    session.add(AuditLog(
        owner_id=owner, action="automation." + action,
        resource_type="automation", resource_id=resource_id,
        details_json=encoded(details or {}),
    ))


def execute_creation_batch(run_ids, identity):
    """Two products at a time; keep queued siblings alive while model work is slow."""
    stop = Event()
    def renew_queue():
        while not stop.wait(30):
            try:
                with db.SessionLocal() as session:
                    session.execute(update(AutomationRun).where(
                        AutomationRun.id.in_(run_ids), AutomationRun.owner_id == identity.user_id,
                        AutomationRun.status == 'queued', AutomationRun.cancel_requested.is_(False),
                    ).values(lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS)))
                    session.commit()
            except Exception:
                return
    heartbeat = Thread(target=renew_queue, daemon=True)
    heartbeat.start()
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='product-creation') as workers:
            list(workers.map(lambda run_id: execute_run(run_id, identity), run_ids))
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def recover_interrupted() -> int:
    """Expired work never resumes publishing automatically, including across processes."""
    now = utcnow()
    count = 0
    with db.SessionLocal() as session:
        rows = session.scalars(select(AutomationRun).where(
            AutomationRun.status.in_(ACTIVE), AutomationRun.lease_expires_at < now,
        )).all()
        for row in rows:
            previous_results = row.platform_results_json
            results = json.loads(row.platform_results_json)
            uncertain = False
            for value in results.values():
                if value.get("status") == "submitting":
                    value.update(status="unknown", error="提交后执行中断，请到平台核查发布结果")
                    uncertain = True
                elif value.get("status") == "preparing":
                    value.update(status="failed", error="执行中断，尚未提交平台")
            changed = session.execute(update(AutomationRun).where(
                AutomationRun.id == row.id,
                AutomationRun.status == row.status,
                AutomationRun.lease_token == row.lease_token,
                AutomationRun.lease_expires_at < now,
                AutomationRun.platform_results_json == previous_results,
            ).values(
                status="unknown" if uncertain else "failed",
                error="执行中断；请核查平台结果" if uncertain else "执行中断，可以安全重试",
                platform_results_json=encoded(results), lease_token=None,
                lease_expires_at=None, updated_at=now,
            ))
            if changed.rowcount:
                event(session, row, "interrupted", "执行中断，已停止自动发布")
                audit(session, row.owner_id, "interrupted", row.id)
                count += 1
        session.commit()
    return count


class ExecutionStopped(Exception):
    pass


def _owned_active(session, run_id, token):
    row = session.scalar(select(AutomationRun).where(
        AutomationRun.id == run_id, AutomationRun.lease_token == token,
        AutomationRun.status.in_(("preparing", "publishing")),
    ))
    if row is None:
        raise ExecutionStopped()
    return row


def _write_active(session, row, token, values, *, submitting=False):
    """Fence every worker write; a SELECT alone cannot retain ownership of a lease."""
    conditions = [
        AutomationRun.id == row.id,
        AutomationRun.lease_token == token,
        AutomationRun.status == row.status,
        AutomationRun.status.in_(("preparing", "publishing")),
        AutomationRun.platform_results_json == row.platform_results_json,
    ]
    if submitting:
        conditions.extend((
            AutomationRun.status == "publishing",
            AutomationRun.cancel_requested.is_(False),
            AutomationRun.lease_expires_at > utcnow(),
        ))
    changed = session.execute(update(AutomationRun).where(*conditions)
        .values(**values, updated_at=utcnow()).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise ExecutionStopped()


def _heartbeat(run_id, token, stop):
    while not stop.wait(30):
        try:
            with db.SessionLocal() as session:
                changed = session.execute(update(AutomationRun).where(
                    AutomationRun.id == run_id, AutomationRun.lease_token == token,
                    AutomationRun.status.in_(("preparing", "publishing")),
                ).values(lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS)))
                session.commit()
                if not changed.rowcount:
                    return
        except Exception:
            # The boundary callback still requires a live, matching database lease.
            return


def _safe_error(exc, preparing=False):
    from fastapi import HTTPException

    from .llm_adapters import ProviderCallError

    if isinstance(exc, ProviderCallError):
        return str(exc)[:500]
    if hasattr(exc, "code") and hasattr(exc, "message"):
        return str(exc.message)[:500]
    if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
        return exc.detail[:500]
    return "内容准备失败，请检查素材和模型配置后重试" if preparing else "发布执行失败，请检查设备、平台登录和发布配置"


def _finish(run_id, token, *, guided=False):
    with db.SessionLocal() as session:
        row = _owned_active(session, run_id, token)
        label = OPERATION_LABELS[json.loads(row.config_json).get("operation", "publish")]
        states = [value.get("status") for value in json.loads(row.platform_results_json).values()]
        if "unknown" in states or "submitting" in states:
            status = "unknown"
        elif states and all(state == "succeeded" for state in states):
            status = "succeeded"
        elif guided and not row.cancel_requested:
            status = "ready"
        elif "succeeded" in states:
            status = "partial"
        elif row.cancel_requested:
            status = "cancelled"
        else:
            status = "failed"
        _write_active(session, row, token, {
            "status": status, "lease_token": None, "lease_expires_at": None,
        })
        event(session, row, status, {
            "succeeded": f"所有平台已确认{label}成功", "partial": f"部分平台{label}成功，其余平台未完成",
            "unknown": f"平台{label}结果待核查，已禁止自动重试", "cancelled": f"已停止后续{label}",
            "failed": f"{label}未完成，可处理失败原因后重试",
            "ready": "可继续检查并发布尚未完成的平台",
        }[status])
        audit(session, row.owner_id, status, row.id)
        session.commit()


def execute_run(run_id: str, identity: Identity, *, android_platform: str | None = None) -> None:
    """Each run is claimed using a database CAS before any external call."""
    token = str(uuid4())
    with db.SessionLocal() as session:
        claimed = session.execute(update(AutomationRun).where(
            AutomationRun.id == run_id, AutomationRun.owner_id == identity.user_id,
            AutomationRun.status == "queued", AutomationRun.cancel_requested.is_(False),
        ).values(status="preparing", lease_token=token,
                 lease_expires_at=utcnow() + timedelta(seconds=LEASE_SECONDS), updated_at=utcnow()))
        if not claimed.rowcount:
            session.rollback()
            return
        row = session.get(AutomationRun, run_id)
        config, payload = json.loads(row.config_json), json.loads(row.payload_json)
        try:
            config = business_config(config)
        except ValueError as exc:
            error = str(exc)
            _write_active(session, row, token, {
                "status": "failed", "error": error, "lease_token": None, "lease_expires_at": None,
            })
            event(session, row, "business_scope_blocked", error)
            audit(session, row.owner_id, "business_scope_blocked", row.id)
            session.commit()
            return
        if config["purpose"] == "promotion":
            payload = {key: value for key, value in payload.items() if key not in TRANSACTION_FIELDS}
        _write_active(session, row, token, {"config_json": encoded(config)})
        operation = config.get("operation", "publish")
        revising = bool(payload.get('_revision_request'))
        publishing_requested = config["purpose"] != "creation" and not revising and (operation != "publish" or row.publish_requested or (
            config.get("auto_publish", True) and not review.required(config)))
        label = OPERATION_LABELS[operation]
        preparation = (
            "读取商品资料与图片，准备上架文案" if config["purpose"] == "listing"
            else "读取宣传素材，准备推广文案和视频"
        )
        event(session, row, "preparing", preparation if operation == "publish" else f"核对闲鱼商品与账号，准备{label}")
        session.commit()
    runtime_config = dict(config)
    if android_platform:
        if config["execution_mode"] != "android" or android_platform not in config["platforms"]:
            raise ValueError("invalid guided platform")
        runtime_config.update(platforms=[android_platform], _android_run_id=run_id, _android_owner=identity.user_id)
    stop = Event()
    heartbeat = Thread(target=_heartbeat, args=(run_id, token, stop), daemon=True)
    heartbeat.start()
    try:
        try:
            if publishing_requested:
                review.ensure_approved(payload, config)
            if publishing_requested:
                from .automation_executor import preflight
                preflight(runtime_config)
            if revising:
                from .automation_content import revise_content
                payload = revise_content(config, payload, identity)
            elif not payload:
                if operation == "publish":
                    from .automation_content import prepare_content
                    payload = prepare_content(config, identity)
                    review.new_version(payload)
                else:
                    # Existing listings must use the operator's exact identity,
                    # never regenerated copy or a model-selected target.
                    payload = {"target_url": config["target_url"], "title": config["target_title"]}
        except Exception as exc:
            with db.SessionLocal() as session:
                row = _owned_active(session, run_id, token)
                status = "cancelled" if row.cancel_requested else "ready" if android_platform or revising else "failed"
                error = _safe_error(exc, preparing=True)
                failure_values = {
                    "status": status, "error": error,
                    "lease_token": None, "lease_expires_at": None,
                }
                if revising:
                    previous = json.loads(row.payload_json)
                    previous.pop('_revision_request', None)
                    failure_values.update(payload_json=encoded(previous), publish_requested=False)
                _write_active(session, row, token, failure_values)
                event(session, row, status, error)
                audit(session, row.owner_id, status, row.id)
                session.commit()
            return
        with db.SessionLocal() as session:
            row = _owned_active(session, run_id, token)
            if row.cancel_requested:
                status = "cancelled"
            elif not publishing_requested:
                status = "ready"
            else:
                status = "publishing"
            values = {"payload_json": encoded(payload), "status": status}
            if status != "publishing":
                values.update(lease_token=None, lease_expires_at=None)
            _write_active(session, row, token, values)
            if status != "publishing":
                event(session, row, status, "已取消" if status == "cancelled" else "内容已准备完成，等待人工审核入库" if config["purpose"] == "creation" else "内容已准备完成，等待发布")
                session.commit()
                return
            event(session, row, "publishing", f"开始按规则执行{label}")
            session.commit()
        for platform in runtime_config["platforms"]:
            with db.SessionLocal() as session:
                row = _owned_active(session, run_id, token)
                if row.cancel_requested:
                    break
                results = json.loads(row.platform_results_json)
                if results[platform].get("status") in {"succeeded", "unknown", "submitting"}:
                    continue
                results[platform] = {"status": "preparing", "operation": operation}
                _write_active(session, row, token, {"platform_results_json": encoded(results)})
                event(session, row, "platform_preparing", "检查账号并填写发布表单" if operation == "publish" else f"检查账号、目标商品与当前状态，准备{label}", platform)
                session.commit()
            submitted = False

            def before_submit(platform=platform):
                nonlocal submitted
                with db.SessionLocal() as session:
                    row = _owned_active(session, run_id, token)
                    if row.cancel_requested or row.lease_expires_at < utcnow():
                        raise ExecutionStopped()
                    review.ensure_approved(json.loads(row.payload_json), config)
                    results = json.loads(row.platform_results_json)
                    if results[platform].get("status") != "preparing":
                        raise ExecutionStopped()
                    results[platform] = {"status": "submitting", "operation": operation}
                    _write_active(session, row, token, {
                        "platform_results_json": encoded(results),
                    }, submitting=True)
                    event(session, row, "submitting", f"即将提交{label}，后续结果不明时不会自动重试", platform)
                    audit(session, row.owner_id, "submitting", row.id, {"platform": platform, "operation": operation})
                    session.commit()
                submitted = True

            try:
                from .automation_executor import execute, preflight

                preflight(runtime_config)
                connection = {"account_id": config.get("accounts", {}).get(platform, "")}
                if config["execution_mode"] == "android":
                    connection.update(run_id=run_id, owner=identity.user_id)
                receipt = execute(platform, operation, payload, connection,
                    execution_mode=config["execution_mode"], device_id=config.get("device_id"),
                    before_submit=before_submit)
                if not submitted:
                    # A broken adapter reporting completion without the durable marker
                    # cannot establish that no external submission occurred.
                    submitted = True
                    raise RuntimeError("Missing durable publishing boundary")
                if receipt.get("status") != {"publish": "published", "relist": "listed", "delist": "delisted"}[operation]:
                    raise RuntimeError("Missing confirmed publishing boundary or receipt")
                result = {"status": "succeeded", "operation": operation, "receipt": {
                    key: receipt[key] for key in ("status", "url", "external_id", "account_label") if key in receipt
                }}
            except Exception as exc:
                result = {
                    "status": "unknown" if submitted or getattr(exc, "status", "") == "unknown" else "failed",
                    "operation": operation,
                    "error": _safe_error(exc),
                }
            with db.SessionLocal() as session:
                row = _owned_active(session, run_id, token)
                results = json.loads(row.platform_results_json)
                results[platform] = result
                _write_active(session, row, token, {
                    "platform_results_json": encoded(results), "error": result.get("error", ""),
                })
                event(session, row, result["status"], result.get("error", f"平台已确认{label}成功"), platform)
                session.commit()
            if result["status"] == "unknown":
                break
        _finish(run_id, token, guided=bool(android_platform))
    except ExecutionStopped:
        return
    except Exception:
        # Keep the lease and submitting marker intact: stale recovery determines safety.
        return
    finally:
        stop.set()
        heartbeat.join(timeout=2)
