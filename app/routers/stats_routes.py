"""「错题本」页：统计 / 错误列表 / 单条删除 / 偏好规则 / 文本模型连通性测试。

错题本的定位（也是 token 预算的边界）：
  · 它**不会**随通话次数无限膨胀地进 prompt：每通电话前注入的「常犯错」
    是最近 6 通的**聚合数字**（比如 tense×4），不是把每条错误都塞进去。
  · 但错题本本身会越积越多，噪音（uh/um、口音挑刺）会淹没真正要练的错，
    所以这里提供单条删除 + 自动归纳偏好规则：同一类删两次，以后不再记。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..services import llm as llm_svc
from ..services import mistake_rules
from ..services.llm import LLMError
from ..store import db
from .deps import current_user

log = logging.getLogger("sayit.stats_api")

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/errors")
async def list_errors(limit: int = 50, user: str = Depends(current_user)) -> dict[str, Any]:
    return {"errors": db.top_errors(user, limit), "counts": db.error_counts(user)}


@router.delete("/errors/{error_id}")
async def delete_error(error_id: int, user: str = Depends(current_user)) -> dict[str, Any]:
    """从错题本移出一条错题（不物理删，可恢复），并在后台学一次偏好规则。

    学到规则后，下次复盘就不再把同类问题（比如 uh / um 这类思考语气词）记进来。
    """
    row = db.hide_error(user, error_id)
    if not row:
        raise HTTPException(404, "错题本里没有这条")
    # 后台归纳：别让删除按钮等模型 3 秒
    asyncio.create_task(_learn_quietly(user))
    return {"ok": True, "hidden": row.get("you_said", ""), "counts": db.error_counts(user)}


@router.post("/errors/{error_id}/restore")
async def restore_error(error_id: int, user: str = Depends(current_user)) -> dict[str, Any]:
    if not db.restore_error(user, error_id):
        raise HTTPException(404, "没有这条错题")
    return {"ok": True, "counts": db.error_counts(user)}


@router.get("/error-rules")
async def list_rules(user: str = Depends(current_user)) -> dict[str, Any]:
    """AI 从「你删了什么」里学到的偏好规则。"""
    return {"rules": db.list_rules(user), "counts": db.error_counts(user)}


@router.post("/error-rules/learn")
async def learn_rules(user: str = Depends(current_user)) -> dict[str, Any]:
    """手动触发一次归纳（错题本页「再学一次」用）。"""
    return await mistake_rules.learn(user)


@router.delete("/error-rules/{rule_id}")
async def delete_rule(rule_id: int, user: str = Depends(current_user)) -> dict[str, Any]:
    """删掉一条不认同的规则；被它移出的错题会回到错题本。"""
    if not db.delete_rule(user, rule_id):
        raise HTTPException(404, "没有这条规则")
    return {"ok": True, "counts": db.error_counts(user)}


async def _learn_quietly(user: str) -> None:
    try:
        await mistake_rules.learn(user)
    except Exception:
        log.exception("偏好归纳失败（不影响删除）")


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
