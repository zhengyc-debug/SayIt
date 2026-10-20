"""生词本接口：列表 / 词卡（音标 释义 例句 词形）/ 删除。

词卡内容由 app/services/words.py 生成并缓存，这里只做路由与权限。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..services import words as words_svc
from ..store import db
from .deps import current_user

log = logging.getLogger("sayit.words_api")

router = APIRouter(prefix="/api", tags=["words"])


@router.get("/words")
async def list_words(limit: int = 300, user: str = Depends(current_user)) -> dict[str, Any]:
    items = db.list_words(user, max(1, min(500, int(limit))))
    return {"words": items, "total": db.count_words(user)}


@router.get("/words/card")
async def word_card(word: str = Query(..., min_length=1, max_length=64),
                    ui_lang: str = "zh",
                    force: bool = False,
                    user: str = Depends(current_user)) -> dict[str, Any]:
    """取一张词卡。第一次会调文本模型生成并缓存，之后直接返回缓存。"""
    return await words_svc.card(user, word, ui_lang=ui_lang, force=force)


@router.post("/words/card")
async def regenerate_word_card(payload: dict[str, Any],
                               user: str = Depends(current_user)) -> dict[str, Any]:
    word = str(payload.get("word") or "").strip()
    if not word:
        raise HTTPException(400, "缺少 word")
    return await words_svc.card(user, word, ui_lang=str(payload.get("ui_lang") or "zh"),
                                force=True)


@router.delete("/words/{word}")
async def delete_word(word: str, user: str = Depends(current_user)) -> dict[str, Any]:
    ok = db.delete_word(user, word)
    if not ok:
        raise HTTPException(404, "生词本里没有这个词")
    return {"ok": True}
