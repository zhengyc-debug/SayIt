"""登录 / 登出 / 登录态 / healthcheck。"""
from __future__ import annotations

import logging
import platform
import sys
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth
from ..providers import public_providers
from ..store import settings as store
from ..store.settings import LEGACY_OWNER
from .deps import current_user_optional

log = logging.getLogger("sayit.auth_routes")

router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/auth-state")
async def auth_state(request: Request) -> dict[str, Any]:
    """前端启动时问一句「我登录了吗」。未登录也返回 200，不返 401。"""
    user = auth.current_user(request)
    return {"logged_in": user is not None, "user": user}


@router.post("/login")
async def login(request: Request) -> JSONResponse:
    ip = (request.client.host if request.client else "") or "unknown"
    remaining = auth.lock_remaining(ip)
    if remaining:
        return JSONResponse(status_code=429, content={
            "ok": False, "message": f"密码错误次数过多，请 {remaining // 60 + 1} 分钟后再试"})

    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")

    user = auth.check_login(username, password)
    if user is None:
        auth.record_fail(ip)
        log.warning("登录失败 user=%r ip=%s", username[:32], ip)
        return JSONResponse(status_code=200, content={
            "ok": False, "message": "用户名或密码不正确"})

    auth.clear_fails(ip)
    auth.login_session(request, user)
    log.info("登录成功 user=%s ip=%s", user["username"], ip)
    return JSONResponse(content={"ok": True, "user": user,
                                 "next": str(body.get("next") or "/")})


@router.post("/logout")
async def logout(request: Request) -> dict[str, Any]:
    user = auth.current_user(request)
    auth.logout_session(request)
    if user:
        log.info("登出 user=%s", user["username"])
    return {"ok": True}


@router.get("/health")
async def health(user: str | None = None) -> dict[str, Any]:
    """顺带把运行环境报出来 —— 排障第一件要确认的就是"哪个 python 在跑"。"""
    try:
        provider = store.load_settings(user or LEGACY_OWNER)["active"]["provider"]
    except Exception:
        provider = ""
    return {
        "ok": True,
        "provider": provider,
        "providers": [p["id"] for p in public_providers()],
        "runtime": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "prefix": sys.prefix,
            "is_venv": sys.prefix != sys.base_prefix,
            "in_project_venv": "sayit\\.venv" in sys.prefix.lower(),
        },
    }
