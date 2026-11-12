"""Local-only browser login controls; never accept arbitrary destinations."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from . import automation_browser_session as sessions
from .access_routes import PrivateAccessRoute
from .ai_model_config import write_header
from .automation_routes import require_publisher
from .standalone_access import require_local_request


def local_browser_access(request: Request, _identity=Depends(require_publisher)):
    require_local_request(request)


router = APIRouter(
    prefix="/api/v1/automation/browser",
    tags=["promotion browser"],
    route_class=PrivateAccessRoute,
    dependencies=[Depends(local_browser_access)],
)
Platform = Literal["xianyu", "douyin", "xiaohongshu", "kuaishou"]


class OpenPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: Literal["login", "publish"] = "login"


@router.get("/sessions")
def list_sessions(purpose: Literal["listing", "promotion"] | None = None):
    if purpose is None:
        return sessions.sessions()
    platforms = ("xianyu",) if purpose == "listing" else ("douyin", "xiaohongshu", "kuaishou")
    return sessions.sessions(platforms)


@router.post("/platforms/{platform}/open", dependencies=[Depends(write_header)])
def open_platform(platform: Platform, body: OpenPage):
    return sessions.session(platform, open_page=body.page)


@router.post("/platforms/{platform}/inspect", dependencies=[Depends(write_header)])
def inspect_platform(platform: Platform):
    return sessions.session(platform)
