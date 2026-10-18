"""SayIt 服务端装配。

这个文件只做三件事：
  1. 建 app、挂中间件（登录门禁 / 会话 cookie / 静态禁用缓存）
  2. 挂各 router 模块
  3. 启动时初始化数据库、迁移老配置

具体接口按模块分散在 app/routers/：
  auth_routes.py     登录 / 登出 / healthcheck
  settings_routes.py 设置与供应商（每个用户一份）
  call_routes.py     通话 WebSocket + 开场白
  history_routes.py  通话记录 + 复盘报告
  stats_routes.py    错题本 / 统计 / 文本模型测试
  me_routes.py       我的（画像）
  pages_routes.py    前端页面与静态资源

大模型调用在 app/services/（llm / review / profile），
配置与数据库在 app/store/（settings / db）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from . import auth
from .logging_setup import setup_logging
from .paths import DATA_DIR, GREET_DIR
from .routers import (
    auth_routes,
    call_routes,
    history_routes,
    me_routes,
    pages_routes,
    settings_routes,
    stats_routes,
)
from .store import db
from .store import settings as store

setup_logging()
log = logging.getLogger("sayit")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 老版本是全局单文件配置；升级后归到 LEGACY_OWNER 名下，不丢已有 Key
    if store.migrate_legacy(DATA_DIR / "settings.json"):
        log.info("已把旧的全局 data/settings.json 迁移到用户 %s 名下",
                 store.LEGACY_OWNER)
    log.info("SayIt 启动 | 用户配置目录=%s", store.USERS_DIR)
    yield


app = FastAPI(title="SayIt", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """开发期禁用静态资源缓存 —— 否则改了 JS/CSS 刷新看不到效果。"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in (
            "/", "/settings", "/history", "/stats", "/me", "/settings.js"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# 登录门禁：除了登录页本身，所有页面/接口都要求已登录。
# 不用 nginx auth_basic 的原因：那样 WebSocket 拿不到登录态，且弹窗体验差。
# ---------------------------------------------------------------------------
PUBLIC_PATHS = {"/login", "/api/login", "/api/auth-state"}
PUBLIC_PREFIXES = ("/static/",)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    user = auth.current_user(request)
    if user is None:
        # 接口返回 401 让前端跳转，页面直接 302 —— 少一次 JS 往返
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return RedirectResponse(url=f"/login?next={path}", status_code=303)

    request.state.user = user
    return await call_next(request)


# 登录态 = 签名 cookie。
# add_middleware 是 LIFO：最后加的跑在最外层。这里必须放在 require_login
# 之后，否则 SessionMiddleware 在门禁内侧，request.session 直接取不到。
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    session_cookie="sayit_session",
    max_age=auth.COOKIE_MAX_AGE,
    same_site="lax",
    https_only=False,   # 线上走 nginx 的 https，应用只见 http，置 True 会丢 cookie
)

# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
app.include_router(auth_routes.router)
app.include_router(settings_routes.router)
app.include_router(call_routes.router)
app.include_router(history_routes.router)
app.include_router(stats_routes.router)
app.include_router(me_routes.router)
app.include_router(pages_routes.router)

# 开场白音频（模型自己念的录音）
GREET_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/greetings", StaticFiles(directory=str(GREET_DIR)), name="greetings")
