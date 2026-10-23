"""「错题本」页：统计 / 错误列表 / 文本模型连通性测试。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..services import llm as llm_svc
from ..services.llm import LLMError
from ..store import db
from .deps import current_user

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/errors")
async def list_errors(limit: int = 50, user: str = Depends(current_user)) -> dict[str, Any]:
    return {"errors": db.top_errors(user, limit)}


@router.get("/stats")
async def stats(user: str = Depends(current_user)) -> dict[str, Any]:
    return db.stats_overview(user)


@router.delete("/errors")
async def delete_all_errors(user: str = Depends(current_user)) -> dict[str, Any]:
    """清空错题本（前端不弹确认框，所以这里做成一次调用就清干净）。"""
    n = db.delete_all_errors(user)
    return {"ok": True, "deleted": n}


@router.post("/llm/test")
async def llm_test(user: str = Depends(current_user)) -> dict[str, Any]:
    conf = llm_svc.conf_for(user)
    try:
        return {"ok": True, **(await llm_svc.ping(
            base_url=conf.get("base_url", ""), api_key=conf.get("api_key", ""),
            model=conf.get("model", "")))}
    except LLMError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
