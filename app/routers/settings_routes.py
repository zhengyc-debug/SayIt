"""设置与供应商配置接口。

每个用户各有一份配置，所有操作都带 username —— 没有全局 Key。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..languages import LANGUAGES, public_languages, voices_for
from ..prompts import default_name
from ..providers import ProviderSetupError, create_provider, public_providers
from ..scenarios import public_scenarios
from ..store import settings as store
from .deps import current_user

log = logging.getLogger("sayit.settings")

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/bootstrap")
async def bootstrap(user: str = Depends(current_user)) -> dict[str, Any]:
    """前端一次性拿全：可选语言 / 场景 / 供应商 / 当前用户的配置。"""
    s = store.public_settings(user)
    return {
        "active": s["active"],
        "ui": s["ui"],
        # 每门语言下 AI 的默认名字，settings 里配了 assistant_name 时优先用配置
        "names": {code: default_name(code) for code in LANGUAGES},
        "languages": public_languages(),
        "scenarios": public_scenarios(),
        "providers": public_providers(),
        "provider_settings": s["providers"],
        "llm": s.get("llm", {}),
        "user": user,
    }


@router.get("/settings")
async def get_settings(user: str = Depends(current_user)) -> dict[str, Any]:
    return store.public_settings(user)


@router.put("/settings")
async def put_settings(request: Request, user: str = Depends(current_user)) -> dict[str, Any]:
    patch = await request.json()
    return store.apply_public_patch(user, patch)


@router.post("/settings/test")
async def test_provider(request: Request, user: str = Depends(current_user)):
    """连一下就断，验证 Key / 服务开通状态 / 端点是否可用。"""
    body = await request.json() if await request.body() else {}
    conf_all = store.load_settings(user)
    pid = body.get("provider") or conf_all["active"]["provider"]
    conf = store.get_provider_conf(user, pid)
    lang_code = body.get("language") or conf_all["active"]["language"]
    voice = body.get("voice") or (voices_for(lang_code, pid) or [{}])[0].get("id", "")

    if not conf.get("api_key"):
        return {"ok": False, "provider": pid, "code": "no_key",
                "message": "还没有填 API Key —— 先在上面输入你自己的 Key 再测试"}

    prov = None
    try:
        prov = create_provider(pid, conf=conf,
                               instructions="You are a connectivity test. Say OK and stop.",
                               voice=voice, language_code=lang_code)
        t0 = time.perf_counter()
        await prov.connect()
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": True, "provider": pid, "connect_ms": ms,
                "session_id": getattr(prov, "session_id", ""),
                "stats": getattr(prov, "stats", {})}
    except ProviderSetupError as e:
        return JSONResponse(status_code=200, content={
            "ok": False, "provider": pid, "code": e.code,
            "message": e.message, "detail": e.detail})
    except NotImplementedError as e:
        return JSONResponse(status_code=200, content={
            "ok": False, "provider": pid, "code": "not_implemented", "message": str(e)})
    except Exception as e:
        log.exception("测试连接失败")
        return JSONResponse(status_code=200, content={
            "ok": False, "provider": pid, "code": type(e).__name__, "message": str(e)})
    finally:
        if prov is not None:
            try:
                await prov.close()
            except Exception:
                pass
