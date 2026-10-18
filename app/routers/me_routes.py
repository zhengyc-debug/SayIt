"""「我的」页：用户画像 + 手写字段。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..services import profile as profile_svc
from ..store import db
from ..store import settings as store
from .deps import current_user

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/profile")
async def get_profile(user: str = Depends(current_user)) -> dict[str, Any]:
    s = store.active_conf(user)
    snap = profile_svc.snapshot(user, s["language"])
    snap["target_level"] = s["level"]
    return snap


@router.put("/profile")
async def put_profile(request: Request, user: str = Depends(current_user)) -> dict[str, Any]:
    body = await request.json()
    db.update_profile_row(user, display_name=body.get("display_name"),
                          notes=body.get("notes"))
    return await get_profile(user)
