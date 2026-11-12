"""Authenticated device sessions. Credentials and screen contents are never logged."""

import json
import time
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import update
from sqlalchemy.orm import Session

from agent_core import Identity

from . import android_control, android_publishing, db
from .access_routes import PrivateAccessRoute
from .ai_model_config import write_header
from .automation_android import _load_config
from .automation_browser import BrowserPublishError
from .automation_jobs import audit, encoded, event, execute_run
from .automation_models import AutomationRun
from .automation_routes import execution_config, owned, require_publisher, run_document
from .models import utcnow

router = APIRouter(
    prefix="/api/v1/automation/android/devices", tags=["automation"], route_class=PrivateAccessRoute
)


def session_token(x_android_session: str = Header(pattern=r"^[0-9a-f]{32}$")):
    return x_android_session


class OpenScreen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["xianyu"]


class ScreenAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["tap", "swipe", "text", "back", "home", "reopen"]
    frame_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    x: int | None = Field(default=None, ge=0, le=10000, strict=True)
    y: int | None = Field(default=None, ge=0, le=10000, strict=True)
    end_x: int | None = Field(default=None, ge=0, le=10000, strict=True)
    end_y: int | None = Field(default=None, ge=0, le=10000, strict=True)
    text: str | None = Field(default=None, max_length=2000)


class DraftRequest(OpenScreen):
    run_id: str = Field(min_length=1, max_length=64)


class SubmitDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)
    frame_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    media_confirmed: StrictBool


def draft_run(session, run_id, device_id, identity):
    row = owned(session, AutomationRun, run_id, identity)
    execution_config(row.config_json)
    config = json.loads(row.config_json)
    if (
        row.status != "ready"
        or config.get("execution_mode") != "android"
        or config.get("device_id") != device_id
        or config.get("operation", "publish") != "publish"
        or config.get("platforms") != ["xianyu"]
    ):
        raise HTTPException(409, "请选择此设备的待发布草稿")
    if any(
        item.get("status") in {"unknown", "submitting"}
        for item in json.loads(row.platform_results_json).values()
    ):
        raise HTTPException(409, "已有结果不明的提交，请先到平台核查")
    return row, config


@router.post("/{device_id}/draft", dependencies=[Depends(write_header)])
def open_draft(
    device_id: str,
    body: DraftRequest,
    identity: Identity = Depends(require_publisher),
    session: Session = Depends(db.get_session),
):
    row, config = draft_run(session, body.run_id, device_id, identity)
    results = json.loads(row.platform_results_json)
    if (
        body.platform not in config["platforms"]
        or results.get(body.platform, {}).get("status") == "succeeded"
    ):
        raise HTTPException(409, "该平台不在草稿中或已成功发布")
    return call(
        android_publishing.open_draft,
        device_id,
        body.platform,
        identity.user_id,
        row.id,
        json.loads(row.payload_json),
        config.get("accounts", {}).get(body.platform),
    )


@router.post("/{device_id}/fill-draft", dependencies=[Depends(write_header)])
def fill_draft(
    device_id: str,
    token: str = Depends(session_token),
    identity: Identity = Depends(require_publisher),
):
    return call(android_publishing.fill_draft, device_id, token, identity.user_id)


@router.post("/{device_id}/publish-draft", dependencies=[Depends(write_header)], status_code=202)
def publish_draft(
    device_id: str,
    body: SubmitDraft,
    background: BackgroundTasks,
    token: str = Depends(session_token),
    identity: Identity = Depends(require_publisher),
    session: Session = Depends(db.get_session),
):
    if not body.media_confirmed:
        raise HTTPException(409, "请先核对本次素材、账号和发布信息")
    row, workflow = draft_run(session, body.run_id, device_id, identity)

    def reserve(config, identifier):
        with android_control.device_lock(config, identifier):
            platform, account = android_publishing.reserve_submission(
                config,
                identifier,
                token,
                identity.user_id,
                row.id,
                json.loads(row.payload_json),
                body.frame_id,
            )
            if (
                platform not in workflow["platforms"]
                or json.loads(row.platform_results_json).get(platform, {}).get("status")
                == "succeeded"
            ):
                raise HTTPException(409, "该平台已发布或不属于此任务")
            workflow.setdefault("accounts", {})[platform] = account
            changed = session.execute(
                update(AutomationRun)
                .where(
                    AutomationRun.id == row.id,
                    AutomationRun.status == "ready",
                    AutomationRun.payload_json == row.payload_json,
                    AutomationRun.config_json == row.config_json,
                )
                .values(
                    status="queued",
                    config_json=encoded(workflow),
                    publish_requested=True,
                    cancel_requested=False,
                    error="",
                    attempt=row.attempt + 1,
                    updated_at=utcnow(),
                )
            )
            if not changed.rowcount:
                session.rollback()
                raise HTTPException(409, "草稿已变化，请刷新后重新检查")
            event(
                session, row, "android_draft_confirmed", "已核对 App 素材、账号和发布表单", platform
            )
            audit(
                session, identity.user_id, "android_draft_confirmed", row.id, {"platform": platform}
            )
            session.commit()
            session.refresh(row)
            background.add_task(execute_run, row.id, identity, android_platform=platform)
            return run_document(session, row)

    return call(reserve, device_id)


def call(operation, device_id, *args):
    try:
        config = _load_config()
        if device_id not in config["devices"]:
            raise HTTPException(404, "安卓设备不存在")
        return operation(config, device_id, *args)
    except BrowserPublishError as exc:
        raise HTTPException(409, {"code": exc.code, "message": exc.message}) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503, "安卓操作未完成，请刷新屏幕检查当前页面；请勿重复点击提交。"
        ) from None


@router.post("/{device_id}/cleanup-media", dependencies=[Depends(write_header)])
def cleanup_media(device_id: str, _: Identity = Depends(require_publisher)):
    from .android_connection import connect
    from .android_media import cleanup

    def clean(config, identifier):
        with android_control.device_lock(config, identifier):
            state = android_control.read_state(config, identifier)
            if (
                android_control.session_active(state)
                or state.get("prepared", {}).get("expires_at", 0) > time.time()
            ):
                raise HTTPException(409, "请先关闭安卓画面并完成当前发布操作")
            return cleanup(connect(config, identifier), config, identifier, state)

    return call(clean, device_id)


@router.post("/{device_id}/sessions", dependencies=[Depends(write_header)])
def open_screen(device_id: str, body: OpenScreen, identity: Identity = Depends(require_publisher)):
    return call(android_control.open_session, device_id, body.platform, identity.user_id)


@router.get("/{device_id}/screen")
def screen(
    device_id: str,
    token: str = Depends(session_token),
    identity: Identity = Depends(require_publisher),
):
    return call(android_control.screen, device_id, token, identity.user_id)


@router.post("/{device_id}/actions", dependencies=[Depends(write_header)])
def action(
    device_id: str,
    body: ScreenAction,
    token: str = Depends(session_token),
    identity: Identity = Depends(require_publisher),
):
    return call(android_control.control, device_id, token, identity.user_id, body.model_dump())


@router.post("/{device_id}/verify-account", dependencies=[Depends(write_header)])
def verify(
    device_id: str,
    token: str = Depends(session_token),
    identity: Identity = Depends(require_publisher),
):
    return call(android_control.verify_account, device_id, token, identity.user_id)


@router.delete("/{device_id}/sessions", dependencies=[Depends(write_header)])
def close(
    device_id: str,
    token: str = Depends(session_token),
    identity: Identity = Depends(require_publisher),
):
    return call(android_control.close_session, device_id, token, identity.user_id)
