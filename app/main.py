"""SayIt 服务端装配。

这个文件只做三件事：
  1. 建 app、挂中间件（登录门禁 / 会话 cookie / 静态禁用缓存）
  2. 挂各 router 模块
  3. 启动时初始化数据库（建表 / 建 admin / 生成签名密钥）

所有状态（账号、每人配置、通话记录）都在一个 SQLite 文件里，
不再有 users.csv / settings.json / secret.key 这些散落文件。

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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from . import auth
from .logging_setup import setup_logging
from . import paths
from .routers import (
    admin_routes,
    auth_routes,
    call_routes,
    history_routes,
    me_routes,
    pages_routes,
    settings_routes,
    stats_routes,
    words_routes,
)
from . import store as store_seed      # 包级：ensure_dirs / seed_greetings
from .session_middleware import LazySessionMiddleware
from .store import accounts, db
from .store import settings as store

setup_logging()
log = logging.getLogger("sayit")




@asynccontextmanager
async def lifespan(app: FastAPI):
    # 顺序有讲究：
    #   1. 建目录 + 补开场白（卷可能是空的）
    #   2. 建所有表（业务表 + accounts / user_settings / app_meta）
    #   3. 保证有 admin、读/生成签名密钥（中间件第一次请求时才会用到它）
    store_seed.ensure_dirs()
    store_seed.seed_greetings()

    db.init_db()
    accounts.init_accounts()
    store.init_settings(paths.DATA_DIR)
    auth.session_secret()          # 提前把密钥写进库，顺带打日志

    log.info("SayIt 启动 | 数据目录=%s | 库=%s", paths.DATA_DIR, paths.DB_PATH)
    yield


app = FastAPI(title="SayIt", version="0.2.0", lifespan=lifespan)


async def no_cache_static(request: Request, call_next):
    """开发期禁用静态资源缓存 —— 否则改了 JS/CSS 刷新看不到效果。"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in (
            "/", "/settings", "/history", "/stats", "/me", "/settings.js",
            "/admin", "/admin.js",
            "/words", "/words.js", "/words.html", "/theme.js"):
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

    # 后台只给 admin。接口层的细化判定还在 admin_routes 里，
    # 这里挡住页面路由（/admin、/admin.js）和整片 /api/admin/，
    # 非 admin 的用户连页面都拿不到，不会先看到一张空表再报 403。
    if path == "/admin" or path == "/admin.js" or path.startswith("/api/admin/"):
        if not auth.is_admin(user):
            if path.startswith("/api/"):
                return JSONResponse(status_code=403,
                                    content={"ok": False, "error": "需要管理员权限"})
            return RedirectResponse(url="/", status_code=303)

    request.state.user = user
    return await call_next(request)


# 登录态 = 签名 cookie。
# 用 LazySessionMiddleware：密钥存在 sayit.db 里，要等 lifespan 建完表才存在，
# 而 Starlette 只允许在 import 期 add_middleware。中间件在第一次请求时再去库里取。
#
# 装配顺序 = 从外到内（Starlette 用 insert(0)，列表越靠前越在外层）：
#   require_login（最外：先判登录，未授权直接拒）
#     → LazySessionMiddleware（读 cookie，写 request.session）
#       → no_cache_static（最内：只改响应头）
# 顺序搞反会报 AssertionError: SessionMiddleware must be installed。
app.add_middleware(BaseHTTPMiddleware, dispatch=require_login)
app.add_middleware(
    LazySessionMiddleware,
    secret_loader=auth.lazy_secret,
    session_cookie="sayit_session",
    max_age=auth.COOKIE_MAX_AGE,
    same_site="lax",
    https_only=False,   # 线上走 nginx 的 https，应用只见 http，置 True 会丢 cookie
)
app.middleware("http")(no_cache_static)

# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(settings_routes.router)
app.include_router(call_routes.router)
app.include_router(history_routes.router)
app.include_router(stats_routes.router)
app.include_router(words_routes.router)
app.include_router(me_routes.router)
app.include_router(pages_routes.router)

# 开场白音频（模型自己念的录音）
# 开场白音频：走数据目录（镜像里的默认值由 store.seed_greetings 复制过来）
paths.GREET_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/greetings", StaticFiles(directory=str(paths.GREET_DIR)), name="greetings")
