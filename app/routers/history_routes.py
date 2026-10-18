"""「记录」页：通话列表 / 单通详情 / 复盘报告。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..services import review
from ..services.llm import LLMError
from ..store import db
from .deps import current_user

log = logging.getLogger("sayit.history")

router = APIRouter(prefix="/api", tags=["history"])


@router.get("/sessions")
async def list_sessions(limit: int = 30, offset: int = 0,
                        user: str = Depends(current_user)) -> dict[str, Any]:
    limit = max(1, min(100, int(limit)))
    offset = max(0, int(offset))
    return {
        "sessions": db.list_sessions(user, limit, offset),
        "total": db.count_sessions(user),
        "limit": limit,
        "offset": offset,
    }


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
    row = db.get_session(session_id, user)
    if not row:
        raise HTTPException(404, "没有这通电话")
    return row


@router.get("/sessions/{session_id}/report")
async def get_report(session_id: str, ui_lang: str | None = None,
                     user: str = Depends(current_user)) -> dict[str, Any]:
    """取复盘。ui_lang 和缓存里的语言不一致时视为「还没生成」，
    让前端重新生成一份对应母语的解释。"""
    if not db.get_session(session_id, user):
        raise HTTPException(404, "没有这通电话")
    rep = db.get_report(session_id)
    if rep and (not ui_lang or (rep.get("ui_lang") or "zh") == ui_lang):
        return {"ok": True, "status": "ready", "report": rep}
    row = db.get_session(session_id, user)
    if not row:
        raise HTTPException(404, "没有这通电话")
    if not any(t.get("role") == "user" for t in row.get("turns") or []):
        return {"ok": False, "status": "no_speech",
                "message": "这通电话没有你的语音，无法复盘"}
    return {"ok": True, "status": "pending"}


@router.post("/sessions/{session_id}/report")
async def make_report(session_id: str, request: Request,
                      user: str = Depends(current_user)) -> dict[str, Any]:
    body = await request.json() if await request.body() else {}
    body = body or {}
    force = bool(body.get("force"))
    ui_lang = str(body.get("ui_lang") or "zh").lower()
    if not db.get_session(session_id, user):
        raise HTTPException(404, "没有这通电话")
    try:
        rep = await review.generate(user, session_id, force=force, ui_lang=ui_lang)
        return {"ok": True, "status": "ready", "report": rep}
    except LLMError as e:
        return {"ok": False, "status": "error", "message": str(e), "code": "llm"}
    except ValueError as e:
        return {"ok": False, "status": "error", "message": str(e), "code": "data"}
    except Exception as e:
        log.exception("生成复盘失败")
        return {"ok": False, "status": "error", "message": f"{type(e).__name__}: {e}"}
